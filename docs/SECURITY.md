# Security notes

- This explorer is a **local viewer**. It does not store seeds, OAuth
  tokens, or coins.
- Keep RPC on `127.0.0.1`. Do not set `rpcallowip=0.0.0.0` or forward
  port 38442 on your router.
- `explorer.toml` may contain an RPC password. It is gitignored. Use
  `explorer.toml.example` as the public template.
- If you file a GitHub issue, do not paste `.cookie`, `xcoin.conf`
  passwords, `debug.log` wallet paths you care about, or 12-word seeds.
