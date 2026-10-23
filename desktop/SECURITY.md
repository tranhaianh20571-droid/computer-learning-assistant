# Desktop Runtime Contract

Version: 1, 2026-10-23. This contract applies only to an application-owned desktop
installation. Web authentication and existing owner checks remain in force.

- Internal credentials arrive over the supervised process stdin, never argv.
- The desktop process creates a persistent local user during database bootstrap.
  It issues a normal expiring session directly through the owned startup channel.
  There is no anonymous HTTP auto-login endpoint or fixed default password.
- Local HTTP ports listen on loopback. The desktop gateway requires a random
  per-installation credential and validates Host and Origin. Renderer JavaScript
  receives no general command-execution interface.
- External HTTPS links use a browser picker. The renderer receives opaque IDs
  for verified Windows browser registrations or a program explicitly selected
  through the native file dialog. Only the selected executable and one validated
  HTTPS URL argument reach process creation; registry command strings are never
  executed, and no shell command is composed.
- PostgreSQL has a private data directory, generated credentials and separate
  application/memory databases. Initialization is allowed only on an empty owned
  cluster. Unknown schema versions stop startup; no implicit destructive migration.
- The selected user-data directory is restricted to its Windows owner and SYSTEM.
  An interrupted, explicitly marked first initialization is preserved in an
  isolated recovery directory before retry. Existing databases are never reset.
- Model keys remain encrypted by the existing application encryption mechanism.
  Electron safeStorage protects its randomly generated master key on Windows.
- Native classroom executions require AppContainer access restrictions and a Job
  Object. Missing or failed isolation rejects execution. A normal subprocess is
  never a fallback. Network capabilities and private data ACLs are not granted.
- The pinned LLVM build includes a source-visible startup compatibility shim.
  If Windows denies GetLongPathNameW for an already long executable path, it
  returns that supplied path. It does not read another directory, grant file
  access, or change the AppContainer token. File and network isolation are
  exercised with a private sentinel and a real local TCP listener.
- Component URLs and SHA-256 digests are bound into the packaged release manifest.
  Only archives from this repository's release assets are accepted. Extraction
  rejects links and escaping paths. Downloads never execute setup commands.
- Each extracted file is hashed. Later launches verify file type, size and
  modification time, and rehash changed files; older receipts receive a full
  verification once. This cache trusts the owning Windows account, which can
  already replace the application and its receipts. It is not an anti-malware
  boundary against that same account.
- Shutdown stops owned workers before the owned database. Port occupancy alone
  never authorizes termination of another process.
- Installer and runtime diagnostics contain controlled stage/error codes only.
  They do not copy provider responses, questions, credentials, paths or subprocess
  diagnostics into product logs.
- Program replacement and data migration are separate operations. This initial
  release accepts schema version 1 only; unsupported upgrades fail closed.
