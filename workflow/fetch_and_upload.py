import os
import time
import sys
import json
import argparse
import time
import requests
from datetime import datetime
from huggingface_hub import HfApi, upload_file, hf_hub_download, login
from huggingface_hub.utils import HfHubHTTPError

HF_TOKEN = os.environ.get("HF_TOKEN")
HF_REPO_ID = "sayshara/ol_dump"
MANIFEST_PATH = "ol_sync_manifest.json"
CHUNK_SIZE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB

FILES = {
    "ol_dump_authors_latest.txt.gz": "https://openlibrary.org/data/ol_dump_authors_latest.txt.gz",
    "ol_dump_editions_latest.txt.gz": "https://openlibrary.org/data/ol_dump_editions_latest.txt.gz",
    "ol_dump_works_latest.txt.gz": "https://openlibrary.org/data/ol_dump_works_latest.txt.gz"
}

def get_last_modified(url):
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.head(url, allow_redirects=True, timeout=10)
            r.raise_for_status()
            return r.headers.get("Last-Modified")
        except Exception as e:
            print(f"⚠️ HEAD request attempt {attempt} failed: {e}")
            if attempt == max_retries:
                raise
            time.sleep(2 ** attempt)

def get_hf_last_modified(filename, revision="main"):
    try:
        api = HfApi()
        info = api.dataset_info(HF_REPO_ID, token=HF_TOKEN, revision=revision)
        print(f"📁 Files in {revision}: {[s.rfilename for s in info.siblings]}")
        for sibling in info.siblings:
            if sibling.rfilename == filename:
                print(f"📄 Found {filename} in {revision}")
                lfs = getattr(sibling, "lfs", None)
                if lfs and isinstance(lfs, dict):
                    return lfs.get("lastModified", None)
                else:
                    print(f"⚠️ No LFS metadata for {filename} in {revision}")
                    return "<no-lfs>"
    except HfHubHTTPError as e:
        print(f"⚠️ Could not retrieve HF metadata: {e}")
    return None

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return json.load(f)
    return {}

def save_manifest(data):
    with open(MANIFEST_PATH, "w") as f:
        json.dump(data, f, indent=2)

def download_file(filename, url):
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(filename, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return
        except Exception as e:
            print(f"⚠️ Download attempt {attempt} failed: {e}")
            if attempt == max_retries:
                raise
            time.sleep(2 ** attempt)
        with open(filename, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def try_download_from_hf(filename, ol_modified, manifest=None):
    revision = "backup/raw" if filename.endswith(".txt.gz") else "main"
    hf_modified = get_hf_last_modified(filename, revision=revision)
    manifest_modified = manifest.get(filename, {}).get("source_last_modified") if manifest else None
    if hf_modified == ol_modified:
        print(f"🔁 Attempting to reuse {filename} from Hugging Face (via LFS match)")
        reuse_ok = True
    elif hf_modified is None or hf_modified == "<no-lfs>" and manifest_modified == ol_modified:
        print(f"🔁 Attempting to reuse {filename} from Hugging Face (via manifest match)")
        reuse_ok = True
    else:
        print(f"🔄 Hugging Face version outdated or missing (HF: {hf_modified}, OL: {ol_modified})")
        reuse_ok = False

    if reuse_ok:
        print(f"🔁 Attempting to reuse {filename} from Hugging Face")
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                hf_hub_download(
                    repo_id=HF_REPO_ID,
                    revision=revision,
                    filename=filename,
                    repo_type="dataset",
                    token=HF_TOKEN,
                    local_dir=".",
                    local_dir_use_symlinks=False
                )
                print(f"✅ Reused {filename} from Hugging Face")
                return True
            except Exception as e:
                print(f"⚠️ HF download attempt {attempt} failed: {e}")
                if attempt == max_retries:
                    return False
                time.sleep(2 ** attempt)
    else:
        print(f"🔄 Hugging Face version outdated or missing (HF: {hf_modified}, OL: {ol_modified})")
    return False

def ensure_branch_exists(branch="backup/raw"):
    api = HfApi()
    try:
        branches = api.list_repo_refs(repo_id=HF_REPO_ID, repo_type="dataset")
        if branch not in [b.name for b in branches.branches]:
            print(f"➕ Creating branch '{branch}' from 'main'")
            api.create_branch(repo_id=HF_REPO_ID, repo_type="dataset", branch=branch, token=HF_TOKEN)
    except Exception as e:
        print(f"⚠️ Failed to ensure branch '{branch}': {e}")

def upload_with_chunks(path, repo_path, dry_run=False, branch=None):
    api = HfApi()
    file_size = os.path.getsize(path)
    if file_size <= CHUNK_SIZE_BYTES:
        print(f"📤 Uploading {path} to {repo_path} ({file_size / 1e9:.2f} GB)")
        if not dry_run and (branch or path.endswith(".txt.gz")):
            ensure_branch_exists(branch or "backup/raw")
        if not dry_run:
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    upload_file(
                    path_or_fileobj=path,
                    path_in_repo=os.path.basename(repo_path),
                    commit_message="Upload to Hugging Face",
                    revision=branch or ("backup/raw" if path.endswith(".txt.gz") else "main"),
                        repo_id=HF_REPO_ID,
                        repo_type="dataset",
                        token=HF_TOKEN
                        )
                    break
                except Exception as e:
                    print(f"⚠️ Manifest upload attempt {attempt} failed: {e}")
                    if attempt == max_retries:
                        raise
                    time.sleep(2 ** attempt)
    else:
        print(f"⚠️ File {path} > 5GB, uploading in chunks")
        with open(path, "rb") as f:
            chunk_idx = 0
            while True:
                chunk = f.read(CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                chunk_filename = f"{repo_path}" if chunk_idx == 0 and file_size <= CHUNK_SIZE_BYTES else f"{repo_path}.part{chunk_idx}"
                with open(chunk_filename, "wb") as cf:
                    cf.write(chunk)
                print(f"📤 Uploading chunk {chunk_idx}: {chunk_filename}")
                if not dry_run:
                    upload_file(
                        path_or_fileobj=chunk_filename,
                        path_in_repo=chunk_filename,
                        repo_id=HF_REPO_ID,
                        repo_type="dataset",
                        token=HF_TOKEN
                    )
                os.remove(chunk_filename)
                chunk_idx += 1

def handle_download_and_upload(filename, url, manifest, dry_run, keep):
    print(f"🌠 Checking {filename}")
    ol_modified = get_last_modified(url) if not dry_run else "<dry-run-time>"
    last_synced = manifest.get(filename, {}).get("source_last_modified")

    if not dry_run and last_synced == ol_modified and os.path.exists(filename):
        print(f"✅ {filename} already up to date (OL: {ol_modified})")
        return

    print(f"🚀 New version detected or file missing (OL: {ol_modified}, HF: {last_synced})")
    if not dry_run:
        if not os.path.exists(filename):
            print(f"⚠️ File {filename} missing locally. Attempting recovery.")
            reused = try_download_from_hf(filename, ol_modified, manifest=manifest)
            if reused:
                print(f"✅ Reused {filename} from Hugging Face — skipping upload.")
                return
            if not reused:
                print(f"⬇️ Downloading {filename} from OpenLibrary")
                try:
                    download_file(filename, url)
                except Exception as e:
                    print(f"❌ Fallback download from OpenLibrary failed: {e}")
                    return
        if not reused:
            upload_with_chunks(filename, filename, dry_run=dry_run, branch=None)
        if os.path.exists(filename) and not keep and not filename.endswith(".txt.gz"):
            print(f"🧹 Deleting {filename} after upload")
            os.remove(filename)

    if filename not in manifest:
        manifest[filename] = {
            "last_synced": datetime.utcnow().isoformat() + "Z",
            "source_last_modified": ol_modified,
            "converted_chunks": {
                filename: {
                    "last_synced": datetime.utcnow().isoformat() + "Z",
                    "converted": True
                }
            }
        }
    else:
        manifest[filename]["last_synced"] = datetime.utcnow().isoformat() + "Z"
        manifest[filename]["source_last_modified"] = ol_modified
        if "converted_chunks" not in manifest[filename]:
            manifest[filename]["converted_chunks"] = {}
        manifest[filename]["converted_chunks"][filename] = {
            "last_synced": datetime.utcnow().isoformat() + "Z",
            "converted": True
        }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Only process the named file")
    parser.add_argument("--upload-only", help="Only upload the named file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without performing network ops")
    parser.add_argument("--keep", action="store_true", help="Keep downloaded files after upload")
    args = parser.parse_args()

    if not args.dry_run:
        login(token=HF_TOKEN)
    manifest = load_manifest()

    if args.only:
        name = args.only.strip()
        if name in FILES:
            handle_download_and_upload(name, FILES[name], manifest, dry_run=args.dry_run, keep=args.keep)
        else:
            print(f"❌ Unknown file name: {name}")
    elif args.upload_only:
        name = args.upload_only.strip()
        handle_upload_only(name, manifest, dry_run=args.dry_run)
    else:
        for filename, url in FILES.items():
            handle_download_and_upload(filename, url, manifest, dry_run=args.dry_run, keep=args.keep)

    if not args.dry_run:
      save_manifest(manifest)
      max_retries = 3
      for attempt in range(1, max_retries + 1):
          try:
              upload_file(
                  path_or_fileobj=MANIFEST_PATH,
                  path_in_repo=f"metadata/{MANIFEST_PATH}",
                  repo_id=HF_REPO_ID,
                  repo_type="dataset",
                  token=HF_TOKEN
              )
              break
          except Exception as e:
              print(f"⚠️ Manifest upload attempt {attempt} failed: {e}")
              if attempt == max_retries:
                  raise
              time.sleep(2 ** attempt)

    print("\n🌟 Sync complete." + (" (Dry run mode)" if args.dry_run else " Manifest updated and uploaded."))

if __name__ == "__main__":
    main()
