# Pinned Let's Encrypt roots

Optional. `letsencrypt_roots.yaml` is a CA list for MQTT over TLS to a
broker with a Let's Encrypt certificate, e.g. behind a reverse proxy
(`mqtt: certificate_authority:` in `device.yaml`; see
`device.example.yaml`). ESPHome's MQTT client doesn't use ESP-IDF's
certificate bundle, so the roots are pinned here. It's a YAML document
holding one string, so ESPHome can `!include` it. Plain MQTT on your LAN
(the default with Home Assistant's Mosquitto broker app) doesn't need any of this.

| Root | Key | Valid until | SHA-256 fingerprint |
|---|---|---|---|
| ISRG Root X1 | RSA 4096 | 2035-06-04 | `96:BC:EC:06:26:49:76:F3:74:60:77:9A:CF:28:C5:A7:CF:E8:A3:C0:AA:E1:1A:8F:FC:EE:05:C0:BD:DF:08:C6` |
| ISRG Root X2 | ECDSA P-384 | 2040-09-17 | `69:72:9B:8E:15:A8:6E:FC:17:7A:57:AF:B7:17:1D:FC:64:AD:D2:8C:2F:CA:8C:F1:50:7E:34:45:3C:CB:14:70` |
| ISRG Root YR | RSA | 2045-09-02 | `E5:7B:7E:6F:15:0C:41:91:02:E8:D5:C0:55:72:9F:F9:67:B9:D1:A8:29:BF:00:CE:C8:9C:A6:04:EB:F4:A8:6F` |
| ISRG Root YE | ECDSA | 2045-09-02 | `E1:4F:FC:AD:5B:00:25:73:10:06:CA:A4:3A:12:1A:22:D8:E9:70:0F:4F:B9:CF:85:2F:02:A7:08:AA:5D:56:66` |

Provenance (checked 2026-09-29):

- X1 and X2: downloaded from letsencrypt.org/certs/ and byte-identical in
  fingerprint to the Ubuntu system CA store (`/etc/ssl/certs/ISRG_Root_X*.pem`).
- YR and YE (Let's Encrypt's 2025 generation) from letsencrypt.org/certs/gen-y/:
  each self-signed root has the same public key as its cross-signed version
  (`root-yr-by-x1`, `root-ye-by-x2`), and those cross-signs verify against X1
  and X2 respectively.

Why all four: when checked (2026-09-29), a Let's Encrypt RSA certificate
served the chain leaf <- YR1 <- Root YR (cross-signed) <- ISRG Root X1,
which verifies against X1 alone and against YR alone. When Let's Encrypt stops sending the X1 cross-sign, only a device
that also trusts YR keeps connecting. X2/YE cover an ECDSA certificate.
