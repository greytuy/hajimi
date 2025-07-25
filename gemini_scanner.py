import requests
import json
import time
import os
import random
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions

load_dotenv()

# --- PROXY CONFIGURATION ---
# 通过环境变量 PROXY_URL 控制；默认不使用代理
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

if PROXY_URL:
    print(f"Using proxy: {PROXY_URL}")
    os.environ["https_proxy"] = PROXY_URL
    os.environ["http_proxy"] = PROXY_URL
# --- END PROXY CONFIGURATION ---

# GitHub API endpoint for code search
GITHUB_API_URL = "https://api.github.com/search/code"

# 用户可调整：仅搜索最近 N 年内更新过的仓库文件
DATE_RANGE_YEARS = 2
# 计算时间截止点（UTC）
_date_cutoff = (datetime.utcnow() - timedelta(days=365 * DATE_RANGE_YEARS)).strftime('%Y-%m-%d')

# Key 搜索词列表 - 分层策略：核心查询 + 扩展查询
# 基于实战经验："正则+语言+排除词是最精准的"，"py和jupyter是重灾区，js和ts是重灾区"

# 🎯 核心查询 (高价值，必须执行)
CORE_SEARCH_QUERIES = [
    '"AIzaSy" language:python',                  # Python 重灾区
    '"AIzaSy" extension:ipynb',                  # Jupyter Notebook 重灾区  
    '"AIzaSy" language:javascript',              # JavaScript 重灾区
    '"AIzaSy" language:typescript',              # TypeScript 重灾区
    '"AIzaSy" -map -maps -youtube -example -demo -tutorial',  # 通用搜索+排除词
    'GEMINI_API_KEY in:file',                    # 最常见的环境变量名
    'filename:.env "AIzaSy" -example',           # .env 文件 (配置重灾区)
]

# 🔍 扩展查询 (可通过环境变量 ENABLE_EXTENDED_SEARCH=true 启用)
EXTENDED_SEARCH_QUERIES = [
    # 更多环境变量命名模式
    'GOOGLE_API_KEY in:file -map -maps',         # Google API 通用变量名
    'google_api_key in:file -map -maps',         # 小写版本
    'gemini_api_key in:file',                    # 小写gemini变量
    
    # SDK使用模式 (针对实际代码)
    'genai.configure language:python',           # Python GenAI SDK配置
    'google.generativeai language:python',       # Python完整导入
    
    # 特定配置文件
    'filename:.yaml "AIzaSy" -example',          # YAML配置文件
    'filename:.json "AIzaSy" -example -package', # JSON配置，排除package.json
    
    # 语言特定环境变量访问
    'os.environ "AIzaSy" language:python',       # Python环境变量
    'process.env "AIzaSy" language:javascript',  # Node.js环境变量
    'process.env "AIzaSy" language:typescript',  # TypeScript环境变量
    
    # 代码赋值模式
    '"api_key=" "AIzaSy" -example -demo',        # 直接赋值
    '"apiKey:" "AIzaSy" language:javascript',    # JS/TS对象属性
]

# 动态组合搜索查询
def get_search_queries():
    """根据环境变量决定使用核心查询还是扩展查询"""
    queries = CORE_SEARCH_QUERIES.copy()
    
    # 检查是否启用扩展搜索
    enable_extended = os.environ.get("ENABLE_EXTENDED_SEARCH", "").lower() in ("true", "1", "yes")
    if enable_extended:
        queries.extend(EXTENDED_SEARCH_QUERIES)
        print(f"扩展搜索已启用，总查询数: {len(queries)}")
    else:
        print(f"使用核心搜索查询，查询数: {len(queries)} (设置 ENABLE_EXTENDED_SEARCH=true 启用扩展搜索)")
    
    return queries

# 为了向后兼容，保留原变量名
SEARCH_QUERIES = get_search_queries()

# Your GitHub Personal Access Token should be set as an environment variable.
# Using a token increases the rate limit for API requests.
# Example: export GITHUB_TOKEN="your_token_here"
# 支持配置多个 PAT，用逗号分隔
_tok_env = os.environ.get("GITHUB_TOKENS")
if _tok_env:
    GITHUB_TOKENS = [t.strip() for t in _tok_env.split(",") if t.strip()]
else:
    single = os.environ.get("GITHUB_TOKEN")
    # 若从 CI/Secrets 注入的 token 带有换行或空格，先进行 strip()
    GITHUB_TOKENS = [single.strip()] if single and single.strip() else []

# 轮询索引
_token_ptr = 0

def _next_token():
    """返回下一个 GitHub Token，若无则 None"""
    global _token_ptr
    if not GITHUB_TOKENS:
        return None
    tok = GITHUB_TOKENS[_token_ptr % len(GITHUB_TOKENS)]
    _token_ptr += 1
    # 再次 strip()，确保不存在隐藏空白字符
    return tok.strip() if isinstance(tok, str) else tok

# Maximum runtime for the script in minutes. 可通过环境变量覆盖。
# Set to 0 or a negative number to run indefinitely.
try:
    MAX_RUNTIME_MINUTES = int(os.environ.get("MAX_RUNTIME_MINUTES", "60"))
except ValueError:
    MAX_RUNTIME_MINUTES = 60

# ----------------- 增量扫描：检查点文件 -----------------
# 扫描进度保存的文件名
CHECKPOINT_FILE = "checkpoint.json"


def load_checkpoint():
    """加载 checkpoint.json，返回 dict。若不存在则返回初始结构"""
    if os.path.isfile(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 基本字段保障
                data.setdefault("last_scan_time", None)
                data.setdefault("scanned_shas", [])
                return data
        except Exception as e:
            print(f"Warning: 无法读取 {CHECKPOINT_FILE}: {e}. 将重建。")
    # 默认结构
    return {"last_scan_time": None, "scanned_shas": []}


def save_checkpoint(data: dict):
    """将 checkpoint 数据写入文件"""
    try:
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error: 保存 {CHECKPOINT_FILE} 失败: {e}")

# 新增：全局变量用于存储只含 key 的输出文件名
KEYS_ONLY_FILENAME = None

def search_github_for_keys(query, token=None, max_retries=3):
    """
    Searches GitHub for code matching the given query.
    """
    # 使用自定义 UA，避免被判定脚本
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GeminiScanner/1.0"
    }
    # 统一去除 token 两端空白，避免出现非法 Header 错误
    if token:
        token = token.strip()
        headers["Authorization"] = f"token {token}"

    params = {
        "q": query,
        "per_page": 100  # Max results per page
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(GITHUB_API_URL, headers=headers, params=params, timeout=30)
            # 403 也会被 raise_for_status 捕获
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else None
            if status in (403, 429):
                wait = 2 ** attempt + random.uniform(0, 1)
                print(f"[search] Hit rate limit or forbidden (HTTP {status}). Retrying in {wait:.1f}s... (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                # 轮换 token
                token = _next_token()
                if token:
                    headers["Authorization"] = f"token {token}"
                continue
            else:
                print(f"HTTP Error making request to GitHub API: {e}")
                return None
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt
            print(f"Network error: {e}. Retrying in {wait}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
    print("Exceeded maximum retries for query:", query)
    return None

def get_file_content(item):
    """
    Downloads the content of a file from GitHub by first fetching its metadata.
    """
    repo_full_name = item["repository"]["full_name"]
    file_path = item["path"]
    
    # Step 1: Get the file's metadata to find the download_url
    metadata_url = f"https://api.github.com/repos/{repo_full_name}/contents/{file_path}"
    headers = {
        "Accept": "application/vnd.github.v3+json",
    }
    token = _next_token()
    if token:
        headers["Authorization"] = f"token {token}"
    
    try:
        metadata_response = requests.get(metadata_url, headers=headers)
        metadata_response.raise_for_status()
        file_metadata = metadata_response.json()
        
        download_url = file_metadata.get("download_url")
        if not download_url:
            print(f"Warning: Could not find download_url in metadata for {item['html_url']}. Skipping.")
            return None

        # Step 2: Download the actual file content
        content_response = requests.get(download_url, headers=headers)
        content_response.raise_for_status()
        return content_response.text

    except requests.exceptions.RequestException as e:
        print(f"Error downloading file content for {item['html_url']}: {e}")
        return None

def extract_keys_from_content(content):
    """
    Extracts Gemini API keys from a string using regex.
    """
    # Regex for Google AI API Keys (starts with AIzaSy)
    pattern = r'(AIzaSy[A-Za-z0-9\-_]{33})'
    return re.findall(pattern, content)

def validate_gemini_key(api_key):
    """
    Validates a Gemini API key by sending a simple "hi" request.
    Returns True if the key is valid, False otherwise.
    """
    try:
        # Pauses execution for 5 seconds
        time.sleep(5)
        
        # Configure the client with the API key and proxy settings
        genai.configure(
            api_key=api_key,
            transport="rest",
            client_options={"api_endpoint": "generativelanguage.googleapis.com"},
        )
        
        # 使用较新的 Gemini 验证模型
        model = genai.GenerativeModel('gemini-2.5-flash-preview-05-20')
        # Send a simple, low-cost request to verify the key
        response = model.generate_content("hi")
        # If we get a response, the key is valid
        return True
    except (google_exceptions.PermissionDenied, google_exceptions.Unauthenticated) as e:
        print(f"  -> Invalid API Key: {api_key[:10]}... Reason: {e.__class__.__name__}")
        return False
    except Exception as e:
        # Catch other potential exceptions (e.g., network issues, timeout)
        print(f"  -> An unexpected error occurred during key validation: {e}")
        return False

def save_result_to_file(log_filename, repo_name, file_path, file_url, valid_keys):
    """将验证成功的密钥写入指定日志文件"""
    with open(log_filename, "a", encoding="utf-8") as f:
        f.write(f"Repository: {repo_name}\n")
        f.write(f"File: {file_path}\n")
        f.write(f"URL: {file_url}\n")
        for key in valid_keys:
            f.write(f"VALID KEY: {key}\n")
        f.write("-" * 80 + "\n")
    # 新增：同时把 key 追加到单独文件
    global KEYS_ONLY_FILENAME
    if KEYS_ONLY_FILENAME:
        with open(KEYS_ONLY_FILENAME, "a", encoding="utf-8") as kf:
            for key in valid_keys:
                kf.write(f"{key}\n")

def main():
    """
    Main function to run the Gemini API key scanner.
    """
    if not GITHUB_TOKENS:
        print("GitHub token not found. Please set the GITHUB_TOKEN or GITHUB_TOKENS environment variable.")
        print("You can create a token at: https://github.com/settings/tokens")
        return

    start_time = datetime.now()
    print(f"Scan started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 为本次扫描生成独立日志文件
    log_filename = f"found_keys_{start_time.strftime('%Y%m%d_%H%M%S')}.log"
    # 新增：生成只包含 key 的文件名
    global KEYS_ONLY_FILENAME
    KEYS_ONLY_FILENAME = f"found_keys_only_{start_time.strftime('%Y%m%d_%H%M%S')}.txt"
    print(f"Log file: {log_filename}")
    # 打印纯 key 文件名
    print(f"Keys-only file: {KEYS_ONLY_FILENAME}")

    # 动态获取搜索查询列表
    current_queries = get_search_queries()
    print(f"Search queries: {', '.join(current_queries)}")
    if MAX_RUNTIME_MINUTES > 0:
        print(f"Script will run for a maximum of {MAX_RUNTIME_MINUTES} minutes.")

    # 读取 checkpoint 以便增量扫描
    checkpoint = load_checkpoint()
    scanned_shas = set(checkpoint.get("scanned_shas", []))
    last_scan_time_str = checkpoint.get("last_scan_time")
    if last_scan_time_str:
        print(f"增量模式：跳过 {len(scanned_shas)} 个已扫描文件；仅处理仓库 push 时间晚于 {last_scan_time_str} 的结果。")

    # 统计不同查询得到的 item
    aggregated_items = []
    for q in current_queries:
        res = search_github_for_keys(q, _next_token())
        if res and "items" in res:
            aggregated_items.extend(res["items"])

    if aggregated_items:
        total_keys_found = 0
        print(f"Found {len(aggregated_items)} potential files after aggregation. Now scanning contents...")
        for item in aggregated_items:
            if MAX_RUNTIME_MINUTES > 0 and (datetime.now() - start_time) > timedelta(minutes=MAX_RUNTIME_MINUTES):
                print("\nMaximum runtime exceeded. Stopping scan.")
                break

            # 若 checkpoint 启动且仓库 push 早于上次扫描，则跳过
            if last_scan_time_str:
                try:
                    last_scan_dt = datetime.fromisoformat(last_scan_time_str)
                    repo_pushed_at = item["repository"].get("pushed_at")
                    if repo_pushed_at:
                        repo_pushed_dt = datetime.strptime(repo_pushed_at, "%Y-%m-%dT%H:%M:%SZ")
                        if repo_pushed_dt <= last_scan_dt:
                            continue
                except Exception:
                    # 若无法解析则继续后续逻辑
                    pass

            # 如果该文件 sha 已经扫描过，则跳过
            if item.get("sha") in scanned_shas:
                continue

            # 原有：如果仓库最近一次 push 早于时间窗口，则跳过
            repo_pushed_at = item["repository"].get("pushed_at")
            if repo_pushed_at:
                repo_pushed_dt = datetime.strptime(repo_pushed_at, "%Y-%m-%dT%H:%M:%SZ")
                if repo_pushed_dt < datetime.utcnow() - timedelta(days=365 * DATE_RANGE_YEARS):
                    continue

            # 跳过明显的文档或示例文件
            lowercase_path = item["path"].lower()
            if any(token in lowercase_path for token in ["readme", "docs", "doc/", ".md", "example", "sample", "tutorial"]):
                continue

            delay = random.uniform(1, 4)
            file_url = item["html_url"]
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking {file_url} ... (waiting {delay:.2f}s)")
            time.sleep(delay)

            content = get_file_content(item)
            if content:
                keys = extract_keys_from_content(content)

                # 过滤占位符（如 AIzaSy... 带省略号或 YOUR_API_KEY 等）
                filtered_keys = []
                for key in keys:
                    # 如果 key 周围 5 字符内含 "..." 则跳过
                    context_index = content.find(key)
                    if context_index != -1:
                        snippet = content[context_index:context_index+45]
                        if "..." in snippet or "YOUR_" in snippet.upper():
                            continue
                    filtered_keys.append(key)
                keys = filtered_keys

                if keys:
                    print(f"SUCCESS: Found {len(keys)} potential key(s) in {file_url}. Validating...")
                    
                    valid_keys = []
                    for key in keys:
                        if validate_gemini_key(key):
                            valid_keys.append(key)
                            print(f"  -> VALID key found: {key[:10]}...")
                    
                    if valid_keys:
                        total_keys_found += len(valid_keys)
                        repo_name = item["repository"]["full_name"]
                        file_path = item["path"]
                        file_url = item["html_url"]
                        
                        print("-" * 80)
                        save_result_to_file(log_filename, repo_name, file_path, file_url, valid_keys)
                        print(f"Result for {len(valid_keys)} valid key(s) saved to {log_filename}. Total valid keys found so far: {total_keys_found}")
                    else:
                        print("  -> No valid keys found in this file.")

                # 无论找到与否，都记住已扫描 sha
                if item.get("sha"):
                    scanned_shas.add(item["sha"])

    else:
        print("No results found or an error occurred.")

    print("-" * 80)
    print("Scan complete.")

    # 保存新的 checkpoint
    checkpoint["last_scan_time"] = datetime.utcnow().isoformat()
    checkpoint["scanned_shas"] = list(scanned_shas)
    save_checkpoint(checkpoint)

if __name__ == "__main__":
    main()
