# 12C STATELESS RELAY TRANSFER

This is a file transfer system based on 12C protocol that makes files stored stateless on relay servers and metadata undistinguishable. File extraction permission is conveyed using a 12-character out-of-band credential, which is responsible for data indexing and confidentiality.

## Production deployment and packages

- Linux server deployment: [`deploy/README.md`](deploy/README.md)
- The `build-and-test` GitHub Actions workflow runs both backend test suites,
  builds the Web client and produces Windows NSIS and Linux AppImage packages.
- Public deployment uses an explicit route allowlist. Registry/Relay admin APIs,
  Relay registration and heartbeat, Console, health details and OpenAPI are
  internal-only.
