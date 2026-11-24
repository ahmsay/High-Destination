#!/usr/bin/env bash
# Runs once per instance boot (from EC2 user data). Installs ComfyUI, pulls
# model weights from S3 (seeding S3 from the internet on the very first run),
# starts ComfyUI headless and runs the job loop. Whatever happens, the instance
# uploads its log to S3 and terminates itself at the end, so a broken boot can
# never leave a GPU running.
set -euxo pipefail

: "${HD_BUCKET:?}" "${HD_QUEUE_URL:?}" "${AWS_REGION:?}"
HD_DIR=/opt/hd
LOG=/var/log/hd-bootstrap.log

imds() {
  local token
  token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 300")
  curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1"
}
INSTANCE_ID=$(imds instance-id)

finish() {
  set +e
  echo "bootstrap exiting (status $1), shutting down"
  if [ -f /var/log/hd-comfyui.log ]; then
    { echo "===== ComfyUI log (last 300 lines) ====="; tail -n 300 /var/log/hd-comfyui.log; } >> "$LOG"
  fi
  aws s3 cp "$LOG" "s3://$HD_BUCKET/logs/$(date -u +%Y%m%dT%H%M%SZ)-$INSTANCE_ID.log"
  [ -n "${HD_KEEP_INSTANCE:-}" ] && exit 0
  aws autoscaling terminate-instance-in-auto-scaling-group \
    --instance-id "$INSTANCE_ID" --should-decrement-desired-capacity \
    || shutdown -h now
}
trap 'finish $?' EXIT

# ---- Fast local disk ---------------------------------------------------------
# g4dn/g5/g6/g6e come with an NVMe instance store; use it for ComfyUI + weights.
WORK=/opt/hd/work
if mountpoint -q /opt/dlami/nvme; then
  WORK=/opt/dlami/nvme/hd
else
  dev=$(ls /dev/disk/by-id/nvme-Amazon_EC2_NVMe_Instance_Storage_* 2>/dev/null | grep -v -- -part | head -1 || true)
  if [ -n "$dev" ]; then
    mkfs.ext4 -F -q "$dev"
    mkdir -p /mnt/nvme && mount -o noatime "$dev" /mnt/nvme
    WORK=/mnt/nvme/hd
  fi
fi
mkdir -p "$WORK"

# Swap: the xlarge sizes have 16 GB RAM (g6e.xlarge: 32), and loading FLUX can spike above that.
if [ "$(free -g | awk '/Mem:/ {print $2}')" -lt 30 ] && ! swapon --show | grep -q .; then
  fallocate -l 16G "$WORK/swapfile" && chmod 600 "$WORK/swapfile"
  mkswap "$WORK/swapfile" && swapon "$WORK/swapfile"
fi

# ---- ComfyUI -----------------------------------------------------------------
export UV_INSTALL_DIR=/usr/local/bin HOME=/root
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
COMFY="$WORK/ComfyUI"
git clone --depth 1 --branch "${HD_COMFYUI_VERSION:-master}" https://github.com/comfyanonymous/ComfyUI.git "$COMFY"
uv venv --python 3.12 "$WORK/venv"
export VIRTUAL_ENV="$WORK/venv" PATH="$WORK/venv/bin:$PATH"
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
uv pip install -r "$COMFY/requirements.txt" boto3

# ---- Model weights -----------------------------------------------------------
HF_TOKEN=$(aws ssm get-parameter --name /high-destination/hf-token --with-decryption \
  --query Parameter.Value --output text 2>/dev/null || true)
aws s3 sync "s3://$HD_BUCKET/models/" "$COMFY/models/" --only-show-errors
# models.txt: "<path under ComfyUI/models> <download url>"; missing files are
# downloaded once and uploaded to S3 so later boots only copy from S3.
while read -r rel url; do
  [ -z "$rel" ] || [[ "$rel" == \#* ]] && continue
  dest="$COMFY/models/$rel"
  [ -s "$dest" ] && continue
  mkdir -p "$(dirname "$dest")"
  echo "downloading $rel"
  curl -fL --retry 3 ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
  aws s3 cp "$dest" "s3://$HD_BUCKET/models/$rel" --only-show-errors
done < "$HD_DIR/models.txt"

# ---- Run ---------------------------------------------------------------------
(cd "$COMFY" && nohup python main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch \
  > /var/log/hd-comfyui.log 2>&1 &)

export HD_COMFY_DIR="$COMFY" HD_INSTANCE_ID="$INSTANCE_ID"
python "$HD_DIR/worker.py"
