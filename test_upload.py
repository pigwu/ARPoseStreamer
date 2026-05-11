#!/usr/bin/env python3
"""Test script to simulate iPhone CSV upload and show progress"""

import requests
import time
from pathlib import Path

# Create a test CSV file
test_csv = Path("test_pose.csv")
csv_content = "sequence,sender_time,frame_time,relative_time,x,y,z,qx,qy,qz,qw\n"
for i in range(1000):
    csv_content += f"{i},{time.time()},{time.time()},0.0,0.5,0.3,0.8,0.0,0.0,0.0,1.0\n"

test_csv.write_text(csv_content, encoding="utf-8")

print("=" * 70)
print("Testing Upload Server")
print("=" * 70)
print(f"Test file: {test_csv.resolve()}")
print(f"File size: {test_csv.stat().st_size / 1024:.2f} KB")
print("\nUploading to http://localhost:8000/upload ...")
print("=" * 70)

# Upload the file
headers = {
    "X-Capture-ID": "test-session-001",
    "X-Capture-Component": "pose",
    "X-Original-Filename": "pose.csv",
    "X-Upload-Kind": "pose",
}

with open(test_csv, "rb") as f:
    response = requests.post(
        "http://localhost:8000/upload",
        data=f,
        headers=headers,
        timeout=30
    )

print("\n" + "=" * 70)
if response.status_code == 200:
    result = response.json()
    print("Upload successful!")
    print(f"Saved to: {result['saved_to']}")
else:
    print(f"Upload failed with status {response.status_code}")

# Cleanup
test_csv.unlink()
print("=" * 70)
