# AGT remote operator access

This package documents the proven Remote Desktop Commander bridge used for authorized operator access to the AGT host from ChatGPT.

It is deliberately out-of-band from Attention Router runtime and from the GitHub App. It provides terminal/filesystem transport only; it does not grant repository authority and does not invoke Codex by itself.

## Proven baseline

The live proof used:

- Debian 13 host;
- system Node.js 20 preserved for existing workloads;
- isolated Node.js 22 under `/opt/node22`;
- `@wonderwhy-er/desktop-commander@0.2.51`;
- Remote MCP endpoint supplied by Desktop Commander;
- one-time browser device authorization.

Node 22 is isolated because current Desktop Commander dependencies require native WebSocket support and Node 22+.

## Install isolated Node.js

Do not replace the host's system Node.js solely for this bridge.

Example:

```bash
mkdir -p /opt/node22
N_PREFIX=/opt/node22 npx -y n 22

/usr/bin/node -v
/opt/node22/bin/node -v
/opt/node22/bin/node -p 'typeof WebSocket'
```

The last command must print `function`.

## Start the bridge

Use the isolated runtime explicitly:

```bash
PATH=/opt/node22/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  npx -y @wonderwhy-er/desktop-commander@0.2.51 remote
```

Complete the browser device-authorization flow when requested. Keep the process attached while proving the connection.

## Safety boundary

Remote Desktop Commander is an administrative transport. Treat it like SSH-equivalent operator access:

- authorize only a host you control;
- disconnect the device when access is no longer required;
- do not paste or log private keys, tokens or webhook secrets into chat;
- keep secrets in root-owned host-local files;
- use read-only probes before mutations;
- do not run `codex` merely because the bridge can execute shell commands.

Calling ordinary shell tools through this bridge is distinct from launching a Codex task. Any explicit `codex` invocation remains a separate execution path and should be intentional.

This provisioning package does not daemonize the remote bridge. Persistent service management and credential lifecycle must be designed and proven separately before being added here.
