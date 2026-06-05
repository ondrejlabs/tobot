# tools/download_test_data.py
import os
from huggingface_hub import snapshot_download

def download_data():
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("Warning: HF_TOKEN is missing. Skipping data download.")
        return False
    
    try:
        print("Downloading private smoke test data from Hugging Face...")
        snapshot_download(
            repo_id="your-username/tobot-test-data",
            repo_type="dataset",
            local_dir="./tests/data",
            token=token
        )
        return True
    except Exception as e:
        print(f"Error downloading data: {e}")
        return False

if __name__ == "__main__":
    download_data()