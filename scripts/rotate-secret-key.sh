#!/usr/bin/env bash
# scripts/rotate-secret-key.sh — rotate the ScarGuard secrets-at-rest key.
#
# Generates a new key, re-encrypts every enc:v1: field in scarguard.yml
# with the new key, swaps the key file atomically, and restarts affected
# services. Run from the project root with docker compose available.
#
# Usage:
#   scripts/rotate-secret-key.sh
#
# What it does:
#   1. Generates a new 32-byte key → /data/secret_key.new
#   2. Reads scarguard.yml, decrypts with OLD key, re-encrypts with NEW key
#   3. Writes the re-encrypted config back
#   4. Atomically swaps secret_key.new → secret_key
#   5. Restarts web, notifier, deterrent (they re-read key on boot)
#   6. Verifies the config loads cleanly with the new key

set -euo pipefail

COMPOSE="docker compose"
WEB_SERVICE="web"
KEY_PATH="/data/secret_key"

echo "=== ScarGuard Secret Key Rotation ==="
echo ""

# Verify key exists
echo "→ Checking current key"
$COMPOSE exec -T "$WEB_SERVICE" python3 -c "
import secret_box
secret_box.load_key()
print('Current key OK')
" || { echo "ERROR: No current key found at $KEY_PATH. Nothing to rotate."; exit 1; }

# Generate new key, decrypt with old, re-encrypt with new, write config
echo "→ Generating new key and re-encrypting config"
$COMPOSE exec -T "$WEB_SERVICE" python3 -c "
import os, tempfile
import yaml
import secret_box

KEY_PATH = secret_box.DEFAULT_KEY_PATH
NEW_KEY_PATH = KEY_PATH + '.new'
CONFIG_PATH = os.environ.get('CONFIG_PATH', '/config/scarguard.yml')

# Load old key
old_key = secret_box.load_key(KEY_PATH)

# Generate new key to .new file
new_key = secret_box.generate_key()
fd = os.open(NEW_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, new_key)
finally:
    os.close(fd)

# Read config YAML, decrypt with old key
with open(CONFIG_PATH, 'r') as f:
    cfg = yaml.safe_load(f) or {}
decrypted = secret_box.decrypt_in_place(cfg, old_key)
print(f'Decrypted {decrypted} field(s) with old key')

# Re-encrypt with new key
encrypted = secret_box.encrypt_in_place(cfg, new_key)
print(f'Re-encrypted {encrypted} field(s) with new key')

# Atomic write of re-encrypted config
fd2, tmp = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), suffix='.tmp')
with os.fdopen(fd2, 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
os.replace(tmp, CONFIG_PATH)

# Atomic swap of key file
os.replace(NEW_KEY_PATH, KEY_PATH)
print('Key rotated successfully')
" || { echo "ERROR: Re-encryption failed. Old key is still active."; exit 2; }

echo "→ Restarting services"
$COMPOSE restart web notifier deterrent

echo "→ Verifying config loads with new key"
sleep 3
$COMPOSE exec -T "$WEB_SERVICE" python3 -c "
import secret_box, config_store
key = secret_box.load_key()
cfg = config_store.load()
if secret_box.has_plaintext_secrets(cfg):
    print('WARNING: Some secrets are still plaintext')
    raise SystemExit(1)
print('Verification OK — all secrets encrypted with new key')
" || { echo "ERROR: Post-rotation verification failed. Check logs."; exit 3; }

echo ""
echo "=== Key rotation complete ==="
echo "  Old key has been replaced. There is no automatic backup of the old key."
echo "  If you kept a manual backup, destroy it now."
