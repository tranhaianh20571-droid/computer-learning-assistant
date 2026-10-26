# Desktop Runtime Contract

Version: 3, 2026-10-26. This contract applies only to an application-owned desktop
installation. Web authentication and existing owner checks remain in force.

- Internal credentials arrive over the supervised process stdin, never argv.
- The desktop process creates a persistent local user during database bootstrap.
  It issues a normal expiring session directly through the owned startup channel.
  There is no anonymous HTTP auto-login endpoint or fixed default password.
- Desktop startup restores the last session from a Windows safeStorage-encrypted
  record and validates it with the owned backend before showing the workspace.
  Expired, revoked or explicitly signed-out sessions open login; they never
  silently switch to the bootstrap user. Cookie replacements and logout update
  the encrypted record. A transient validation failure stops restoration for retry.
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
  A packaged upgrade may transactionally advance the owned desktop baseline from
  versions 1 or 2 to 3, adding course preparation tables when needed and private
  classroom recording metadata. Version 1 also receives the persistent cache flag.
  The version changes only after all additions succeed; existing records survive.
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
- Classroom capture requires a fresh main-frame request from the local lecture
  page and captures only the application frame. Application audio is mixed locally;
  microphone and system loopback capture are not requested. Capture starts automatically
  with classroom playback; route changes stop capture before another page is rendered.
  Returning to the same lecture creates another ordered recording part after the previous
  part has finished saving. Read-only video review never starts classroom recording.
  Video chunks use owned HTTP APIs with
  bounded payloads, sequences and idempotent hashes, never arbitrary file IPC.
- Board export renders a bounded snapshot in a sandboxed, hidden local window.
  Only that window can read its snapshot or announce readiness. Files are written
  only to a native save-dialog destination; no renderer-supplied script is executed.
- Program replacement and data migration are separate operations. This
  release accepts schema version 3 and upgrades owned versions 1 and 2;
  unsupported upgrades fail closed. This path never migrates a development database.
