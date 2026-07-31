# Security policy

## Supported versions

The latest tagged 0.x release receives security fixes while the project is in
preview.

## Reporting a vulnerability

Do not open a public issue containing controller secrets, subscription URLs,
proxy credentials, private node names, or exit IPs. Open a GitHub private
security advisory for this repository.

## Security model

- The daemon is local and uses a Mihomo Unix-domain controller socket by
  default.
- Controller credentials are read at runtime and are never returned by CLI or
  MCP tools.
- MCP version 0.1 uses stdio only and does not expose a network listener.
- State-changing MCP tools require an explicit confirmation value enforced in
  code.
- Runtime files are created with user-only permissions where the platform
  supports them.

Do not configure Mihomo's external controller to listen on a public interface
for this project.
