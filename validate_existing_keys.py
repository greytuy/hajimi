import re
from gemini_scanner import validate_gemini_key

def extract_keys_from_log(log_file="found_keys.log"):
    """Extracts all keys from the log file."""
    keys = set()  # Use a set to avoid duplicate keys
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("FOUND KEY:"):
                    # Extract the key, which is the third part after splitting
                    match = re.search(r'FOUND KEY: (AIzaSy[A-Za-z0-9\-_]{33})', line)
                    if match:
                        keys.add(match.group(1))
    except FileNotFoundError:
        print(f"Error: {log_file} not found.")
        return []
    return list(keys)

def main():
    """Main function to validate keys from the log file."""
    print("Starting validation of keys from found_keys.log...")
    keys_to_validate = extract_keys_from_log()

    if not keys_to_validate:
        print("No keys found in the log file to validate.")
        return

    print(f"Found {len(keys_to_validate)} unique keys to validate.")
    
    valid_keys_count = 0
    invalid_keys_count = 0

    for key in keys_to_validate:
        print(f"\nValidating key: {key[:10]}...")
        if validate_gemini_key(key):
            print(f"  -> RESULT: VALID")
            valid_keys_count += 1
        else:
            print(f"  -> RESULT: INVALID")
            invalid_keys_count += 1
            
    print("\n" + "="*30)
    print("Validation Summary")
    print("="*30)
    print(f"Total Unique Keys Checked: {len(keys_to_validate)}")
    print(f"Valid Keys: {valid_keys_count}")
    print(f"Invalid Keys: {invalid_keys_count}")
    print("="*30)


if __name__ == "__main__":
    main()