import boto3
import os
from pathlib import Path

s3 = boto3.client(
    's3',
    endpoint_url='https://kayhqqrnfxgfdxwmizzz.supabase.co/storage/v1/s3',
    aws_access_key_id='87084c42131c79f5e30b03ef33a8f83b',
    aws_secret_access_key='8a84200415fa90497229211ae3a509ae46e1c96835c67b2059e36ae712a6e516',
    region_name='ap-southeast-1',
)

media_root = Path('media')
bucket = 'media'

for file_path in media_root.rglob('*'):
    if file_path.is_file():
        key = str(file_path.relative_to(media_root)).replace('\\', '/')
        try:
            print(f"Uploading {key}...")
            s3.upload_file(str(file_path), bucket, key, ExtraArgs={'ACL': 'public-read'})
        except Exception as e:
            print(f"SKIPPED {key}: {e}")

print("All done!")

