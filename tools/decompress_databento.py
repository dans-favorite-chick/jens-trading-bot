"""
Databento .zst decompressor — bypasses 7-Zip right-click issues.
Auto-installs zstandard, finds the file, extracts to data/historical/.
"""
import os
import sys
import glob
import subprocess

# Self-install zstandard if needed
try:
    import zstandard
except ImportError:
    print("Installing zstandard library (one-time)...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "zstandard"])
    import zstandard

# Paths
DOWNLOAD_DIR = r"C:\Users\Trading PC\Downloads\GLBX-20260518-JAQG33F8LE"
OUTPUT_DIR = r"C:\Trading Project\phoenix_bot\data\historical"

# Find the .zst file (handle both visible and hidden extension cases)
print(f"Searching for .zst file in: {DOWNLOAD_DIR}")
candidates = []

# Look for .zst files
candidates.extend(glob.glob(os.path.join(DOWNLOAD_DIR, "*.zst")))

# Also look for files starting with "glbx" that aren't JSON
if not candidates:
    for f in os.listdir(DOWNLOAD_DIR):
        full = os.path.join(DOWNLOAD_DIR, f)
        if f.lower().startswith("glbx") and not f.endswith(".json"):
            # Check if it's a binary file (likely zst)
            if os.path.getsize(full) > 1_000_000:  # > 1 MB = data file
                candidates.append(full)

if not candidates:
    print(f"\n❌ No .zst file found in {DOWNLOAD_DIR}")
    print("Files in folder:")
    for f in os.listdir(DOWNLOAD_DIR):
        size_kb = os.path.getsize(os.path.join(DOWNLOAD_DIR, f)) / 1024
        print(f"  {f}  ({size_kb:.1f} KB)")
    sys.exit(1)

zst_file = candidates[0]
print(f"✅ Found: {os.path.basename(zst_file)}")
print(f"   Size: {os.path.getsize(zst_file) / 1024 / 1024:.1f} MB compressed")

# Create output directory
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Build output filename — strip .zst if present, ensure .csv extension
base = os.path.basename(zst_file)
if base.endswith(".zst"):
    output_name = base[:-4]  # remove .zst
else:
    output_name = base + ".csv"

if not output_name.endswith(".csv"):
    output_name += ".csv"

output_path = os.path.join(OUTPUT_DIR, output_name)
print(f"\n📂 Extracting to: {output_path}")

# Decompress
dctx = zstandard.ZstdDecompressor()
with open(zst_file, "rb") as fh_in:
    with open(output_path, "wb") as fh_out:
        dctx.copy_stream(fh_in, fh_out)

# Verify
final_size_mb = os.path.getsize(output_path) / 1024 / 1024
print(f"\n✅ DONE!")
print(f"   Output file: {output_path}")
print(f"   Uncompressed size: {final_size_mb:.1f} MB")

# Peek at the first few lines to confirm it's valid CSV
print(f"\n📋 First 5 lines preview:")
with open(output_path, "r", encoding="utf-8", errors="replace") as f:
    for i, line in enumerate(f):
        if i >= 5:
            break
        print(f"   {line.rstrip()}")

print(f"\n🎯 Ready to use! File at: {output_path}")
