#!/usr/bin/env python3
"""
Decrypt stagewise auth-session.json (Windows DPAPI)

Electron safeStorage on Windows encrypts data using DPAPI.
The encryption key is stored in:
  %APPDATA%/stagewise/session/Local State -> os_crypt.encrypted_key

Decryption chain:
  1. Read Local State -> extract os_crypt.encrypted_key (base64)
  2. Strip "DPAPI" prefix (5 bytes)
  3. DPAPI CryptUnprotectData -> get AES master key
  4. Read auth-session.json (binary)
  5. Strip "v10" prefix (3 bytes)
  6. AES-256-GCM decrypt with master key + nonce

Usage:
  python decrypt_session.py [--save]

  --save  Also save decrypted token to ~/.stagewise-proxy/config.json

Requirements:
  pip install cryptography pywin32
  (pywin32 optional; falls back to PowerShell if not installed)
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path


def dpapi_decrypt(encrypted):
    try:
        import win32crypt
        result = win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)
        if isinstance(result, tuple):
            return result[1]
        return result
    except ImportError:
        pass
    except Exception as e:
        print(f"  win32crypt failed ({e}), trying PowerShell fallback...")

    b64_input = base64.b64encode(encrypted).decode("ascii")
    ps_cmd = (
        "Add-Type -AssemblyName System.Security; "
        f"$b = [Convert]::FromBase64String('{b64_input}'); "
        "$r = [System.Security.Cryptography.ProtectedData]::Unprotect("
        "$b, $null, 'CurrentUser'); "
        "[Convert]::ToBase64String($r)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise OSError(f"DPAPI decrypt failed: {result.stderr[:300]}")
    return base64.b64decode(result.stdout.strip())


def get_master_key(local_state_path):
    with open(local_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    encrypted_key_b64 = data["os_crypt"]["encrypted_key"]
    encrypted_key = base64.b64decode(encrypted_key_b64)

    if encrypted_key[:5] != b"DPAPI":
        raise ValueError("Expected DPAPI prefix in encrypted key")

    return dpapi_decrypt(encrypted_key[5:])


def decrypt_value(master_key, encrypted_value):
    if encrypted_value[:3] not in (b"v10", b"v20"):
        try:
            return encrypted_value.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Unknown encryption prefix")

    nonce = encrypted_value[3:15]
    ciphertext_and_tag = encrypted_value[15:]

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes = AESGCM(master_key)
    return aes.decrypt(nonce, ciphertext_and_tag, None).decode("utf-8")


def main():
    appdata = os.environ.get("APPDATA", "")
    session_dir = Path(appdata) / "stagewise" / "session"
    data_dir = Path(appdata) / "stagewise" / "stagewise"

    local_state_path = session_dir / "Local State"
    auth_session_path = data_dir / "auth-session.json"
    identity_path = data_dir / "identity.json"

    print("=" * 52)
    print("   stagewise Session Decryptor (Windows DPAPI)")
    print("=" * 52)
    print()

    if not local_state_path.exists():
        print(f"X Local State not found: {local_state_path}")
        sys.exit(1)

    if not auth_session_path.exists():
        print(f"X auth-session.json not found: {auth_session_path}")
        sys.exit(1)

    try:
        master_key = get_master_key(str(local_state_path))
        print(f"OK Master key extracted ({len(master_key)} bytes)")
    except Exception as e:
        print(f"X Failed to extract master key: {e}")
        print("  Requirements: pip install cryptography pywin32")
        sys.exit(1)

    with open(auth_session_path, "rb") as f:
        raw = f.read()

    print(f"OK Read auth-session.json ({len(raw)} bytes)")

    try:
        decrypted = decrypt_value(master_key, raw)
        print("OK Decrypted auth-session.json")
        print()
    except Exception as e:
        print(f"X AES-GCM decryption failed: {e}")
        print("  Trying as plain text...")
        try:
            decrypted = raw.decode("utf-8")
        except Exception:
            print("X Not plain text either. Aborting.")
            sys.exit(1)

    try:
        session_data = json.loads(decrypted)
        print("Session data:")
        print(json.dumps(session_data, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print("Decrypted content (not JSON):")
        print(decrypted[:500])
        session_data = None

    if identity_path.exists():
        print()
        with open(identity_path, "r", encoding="utf-8") as f:
            identity = json.load(f)
        print("Machine ID:")
        print(json.dumps(identity, indent=2, ensure_ascii=False))

    save = "--save" in sys.argv
    if save and session_data:
        token = session_data.get("token") or session_data.get("session", {}).get("token")
        if token:
            config_dir = Path.home() / ".stagewise-proxy"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.json"
            config = {"token": token, "user": session_data.get("user"), "port": 11434}
            config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), "utf-8")
            print(f"\nOK Token saved to {config_path}")
            print(f"  Token: {token[:30]}...")
        else:
            print("\nX No token found in decrypted session data")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
