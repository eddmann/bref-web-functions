#!/usr/bin/env node
// Build a deterministic Web Function ZIP from a Bref /opt export, the Rust
// gateway and an application directory.
//
//   node scripts/package.mjs --runtime-dir <opt> --rust-binary <gateway> \
//     --architecture aarch64 [--app-dir <app>] [--keep-dotenv] --output app.zip
//
// The builder validates the archive's structure and ELF machine types. It does
// not run the artifact, test native library resolution, or contact AWS.
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import zlib from 'node:zlib';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MACHINES = { x86_64: 62, aarch64: 183 };
const MAX_BYTES = 512 * 1024 * 1024;
const SYSTEM_CERT_LINK = ['runtime/bref/ssl/cert.pem', '/etc/ssl/cert.pem'];
const IGNORED_DIRS = new Set(['.git', '.hg', '.svn', '.aws', '.ssh', '.idea', '.vscode', '.github',
  'node_modules', 'tests', 'test', 'target', 'build', 'results', '__pycache__']);
// Always omitted. Dotenv files (.env, .env.*) are omitted unless --keep-dotenv.
const SECRET_NAMES = new Set(['.npmrc', '.netrc', '.pypirc', 'auth.json', 'composer-auth.json',
  'id_rsa', 'id_ed25519']);
const RUNTIME_FILES = [
  ['runtime/launcher/web-entry.js', 'index.js'],
  ['runtime/launcher/web-entry.cjs', 'launcher/entry.cjs'],
  ['runtime/launcher/index.js', 'launcher/index.cjs'],
  ['runtime/launcher/config.cjs', 'launcher/config.cjs'],
  ['runtime/php/bootstrap.php', 'compat/bootstrap.php'],
  ['runtime/php/configure.php', 'compat/configure.php'],
  ['runtime/php/init.php', 'compat/init.php'],
];

const isCertLink = (name, target) => name === SYSTEM_CERT_LINK[0] && target === SYSTEM_CERT_LINK[1];

function relativePath(value, allowDot = false) {
  if (allowDot && value === '.') return '.';
  const parts = value.split('/').filter((part) => part !== '' && part !== '.');
  if (value.startsWith('/') || value.includes('\\') || parts.includes('..') || parts.length === 0) {
    throw new Error(`Expected a safe relative package path: ${value}`);
  }
  return parts.join('/');
}

function elfMachine(data, name, architecture) {
  if (data.length < 4 || data.subarray(0, 4).toString('latin1') !== '\x7fELF') return null;
  if (data.length < 20 || data[4] !== 2 || data[5] !== 1) {
    throw new Error(`Unsupported or truncated 64-bit ELF: ${name}`);
  }
  const machine = data.readUInt16LE(18);
  if (machine !== MACHINES[architecture]) {
    throw new Error(`ELF architecture mismatch for ${name}: e_machine=${machine}, expected ${MACHINES[architecture]}`);
  }
  return machine;
}

// Python fnmatch semantics, which existing --exclude patterns rely on: `*` also
// matches `/`.
function globToRegExp(pattern) {
  let source = '';
  for (let i = 0; i < pattern.length; i++) {
    const char = pattern[i];
    if (char === '*') source += '.*';
    else if (char === '?') source += '.';
    else if (char === '[' && pattern.indexOf(']', i + 2) !== -1) {
      const end = pattern.indexOf(']', i + 2);
      let set = pattern.slice(i + 1, end).replaceAll('\\', '\\\\');
      if (set.startsWith('!')) set = `^${set.slice(1)}`;
      source += `[${set}]`;
      i = end;
    } else source += char.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${source}$`, 's');
}

function excludedFromApp(relative, isDirectory, patterns, keepDotenv) {
  const name = path.posix.basename(relative);
  if (isDirectory && IGNORED_DIRS.has(name)) return true;
  if (!isDirectory && !keepDotenv && (name === '.env' || name.startsWith('.env.'))) return true;
  if (!isDirectory && (SECRET_NAMES.has(name) || name.endsWith('.log'))) return true;
  return patterns.some((pattern) => pattern.test(relative));
}

class Archive {
  constructor(architecture) {
    this.architecture = architecture;
    this.entries = new Map();
  }

  add(name, data, mode = 0o644, kind = 'file') {
    name = relativePath(name);
    if (this.entries.has(name)) throw new Error(`Package path collision: ${name}`);
    const entry = { data, mode, kind };
    if (kind === 'file') {
      const machine = elfMachine(data, name, this.architecture);
      if (machine !== null) entry.elfMachine = machine;
    }
    this.entries.set(name, entry);
  }

  /** Adds a file or symlink; `sourceRoot` confines an application's symlinks. */
  source(source, name, sourceRoot = null) {
    const info = fs.lstatSync(source);
    if (info.isSymbolicLink()) {
      let target = fs.readlinkSync(source);
      if (!isCertLink(name, target)) {
        // Bref's /opt links become relative links inside the relocated runtime.
        if (target.startsWith('/opt/') && name.startsWith('runtime/')) {
          target = path.posix.relative(path.posix.dirname(name), `runtime/${target.slice('/opt/'.length)}`);
        } else if (target.startsWith('/')) {
          throw new Error(`External absolute symlink is not allowed: ${name} -> ${target}`);
        }
        if (sourceRoot !== null) {
          const resolved = realpathLoose(path.resolve(path.dirname(source), fs.readlinkSync(source)));
          if (resolved !== sourceRoot && !resolved.startsWith(sourceRoot + path.sep)) {
            throw new Error(`Application symlink escapes its staging directory: ${name}`);
          }
        }
        const resolvedName = path.posix.normalize(path.posix.join(path.posix.dirname(name), target));
        if (resolvedName === '..' || resolvedName.startsWith('../')) {
          throw new Error(`Symlink escapes archive: ${name} -> ${target}`);
        }
      }
      this.add(name, Buffer.from(target), 0o777, 'symlink');
    } else if (info.isFile()) {
      this.add(name, fs.readFileSync(source), info.mode & 0o111 ? 0o755 : 0o644);
    } else {
      throw new Error(`Special files cannot be packaged: ${source}`);
    }
  }

  tree(source, prefix = '', { app = false, excluded = [], keepDotenv = false } = {}) {
    const root = fs.realpathSync(source);
    const walk = (directory) => {
      for (const dirent of fs.readdirSync(directory, { withFileTypes: true })) {
        const item = path.join(directory, dirent.name);
        const relative = path.relative(root, item).split(path.sep).join('/');
        const name = prefix ? `${prefix}/${relative}` : relative;
        // A symlink to a directory counts as a directory but is packaged as a link.
        const isDirectory = dirent.isDirectory() || (dirent.isSymbolicLink() && isDirectoryTarget(item));
        if (app && excludedFromApp(relative, isDirectory, excluded, keepDotenv)) continue;
        if (dirent.isDirectory()) walk(item);
        else if (app || !['bootstrap', 'bref/bootstrap.php'].includes(relative)) {
          this.source(item, name, app ? root : null);
        }
      }
    };
    walk(root);
  }

  validate() {
    let total = 0;
    for (const [name, entry] of this.entries) {
      total += entry.data.length;
      const parts = name.split('/');
      for (let i = 1; i < parts.length; i++) {
        if (this.entries.has(parts.slice(0, i).join('/'))) {
          throw new Error(`Archive entry has a file or symlink parent: ${name}`);
        }
      }
      if (entry.kind !== 'symlink') continue;
      const target = entry.data.toString();
      if (isCertLink(name, target)) continue;
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(name), target)).replace(/\/$/, '');
      if (!this.entries.has(resolved) && ![...this.entries.keys()].some((item) => item.startsWith(`${resolved}/`))) {
        throw new Error(`Dangling archive symlink: ${name} -> ${target}`);
      }
    }
    if (total > MAX_BYTES) {
      throw new Error(`Archive contents exceed the 512 MiB Web Function package limit: ${total} bytes`);
    }
  }

  manifest(configuration, options) {
    const files = {};
    for (const name of [...this.entries.keys()].sort()) {
      const { data, kind, mode, elfMachine: machine } = this.entries.get(name);
      const record = { sha256: sha256(data), bytes: data.length, kind, mode: `0o${mode.toString(8)}` };
      if (machine !== undefined) record.elf_machine = machine;
      if (kind === 'symlink') {
        record.target = data.toString();
        if (isCertLink(name, record.target)) record.external_system_certificate = true;
      }
      files[name] = record;
    }
    return {
      schema: 1, architecture: this.architecture, managed_runtime: 'nodejs24.x', managed_node_included: false,
      deployment_target: this.architecture === 'aarch64' ? 'lambda-web' : 'local-test-only',
      validation: { elf_machine: 'checked', native_execution: 'not performed by builder', aws_execution: 'not performed' },
      configuration, options, files,
    };
  }

  /** A deflated ZIP with fixed timestamps and Unix modes, written atomically. */
  write(destination) {
    const names = [...this.entries.keys()].sort();
    if (names.length > 0xffff) throw new Error(`Too many archive entries for a ZIP without ZIP64: ${names.length}`);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    const temporary = path.join(path.dirname(destination), `.${path.basename(destination)}.${process.pid}.tmp`);
    const descriptor = fs.openSync(temporary, 'w');
    try {
      const DOS_DATE = (1 << 5) | 1; // 1980-01-01, the earliest ZIP date; time 00:00
      const central = [];
      let offset = 0;
      for (const name of names) {
        const { data, mode, kind } = this.entries.get(name);
        const nameBytes = Buffer.from(name);
        const flags = /^[\x00-\x7f]*$/.test(name) ? 0 : 0x800; // UTF-8 name
        const compressed = zlib.deflateRawSync(data, { level: 9 });
        const crc = crc32(data);
        const local = Buffer.alloc(30);
        local.writeUInt32LE(0x04034b50, 0);
        local.writeUInt16LE(20, 4);
        local.writeUInt16LE(flags, 6);
        local.writeUInt16LE(8, 8);
        local.writeUInt16LE(DOS_DATE, 12);
        local.writeUInt32LE(crc, 14);
        local.writeUInt32LE(compressed.length, 18);
        local.writeUInt32LE(data.length, 22);
        local.writeUInt16LE(nameBytes.length, 26);
        fs.writeSync(descriptor, local);
        fs.writeSync(descriptor, nameBytes);
        fs.writeSync(descriptor, compressed);
        const header = Buffer.alloc(46);
        header.writeUInt32LE(0x02014b50, 0);
        header.writeUInt16LE((3 << 8) | 20, 4); // made by Unix
        header.writeUInt16LE(20, 6);
        header.writeUInt16LE(flags, 8);
        header.writeUInt16LE(8, 10);
        header.writeUInt16LE(DOS_DATE, 14);
        header.writeUInt32LE(crc, 16);
        header.writeUInt32LE(compressed.length, 20);
        header.writeUInt32LE(data.length, 24);
        header.writeUInt16LE(nameBytes.length, 28);
        header.writeUInt32LE((((kind === 'symlink' ? 0o120000 : 0o100000) | mode) << 16) >>> 0, 38);
        header.writeUInt32LE(offset, 42);
        central.push(header, nameBytes);
        offset += local.length + nameBytes.length + compressed.length;
      }
      const directory = Buffer.concat(central);
      const end = Buffer.alloc(22);
      end.writeUInt32LE(0x06054b50, 0);
      end.writeUInt16LE(names.length, 8);
      end.writeUInt16LE(names.length, 10);
      end.writeUInt32LE(directory.length, 12);
      end.writeUInt32LE(offset, 16);
      fs.writeSync(descriptor, directory);
      fs.writeSync(descriptor, end);
      fs.closeSync(descriptor);
      fs.renameSync(temporary, destination);
    } catch (error) {
      fs.closeSync(descriptor);
      fs.rmSync(temporary, { force: true });
      throw error;
    }
  }
}

function isDirectoryTarget(item) {
  try {
    return fs.statSync(item).isDirectory();
  } catch {
    return false; // dangling: packaged as a link and rejected by validate()
  }
}

function realpathLoose(target) {
  try {
    return fs.realpathSync(target);
  } catch {
    return target;
  }
}

const sha256 = (data) => createHash('sha256').update(data).digest('hex');

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(data) {
  if (zlib.crc32) return zlib.crc32(data); // Node 20.15+ and 22.2+
  let crc = 0xffffffff;
  for (const byte of data) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/** JSON with sorted keys, so the manifest is stable. */
function stableJson(value) {
  const sort = (item) => {
    if (Array.isArray(item)) return item.map(sort);
    if (item === null || typeof item !== 'object') return item;
    return Object.fromEntries(Object.keys(item).sort().map((key) => [key, sort(item[key])]));
  };
  return `${JSON.stringify(sort(value), null, 2)}\n`;
}

function build(options) {
  const architecture = options.architecture;
  if (!(architecture in MACHINES)) throw new Error(`--architecture must be one of ${Object.keys(MACHINES).join(', ')}`);
  for (const required of ['runtime-dir', 'rust-binary', 'output']) {
    if (!options[required]) throw new Error(`--${required} is required`);
  }
  const controller = relativePath(options['front-controller']);
  const documentRoot = relativePath(options['document-root'], true);
  const runtime = path.resolve(options['runtime-dir']);
  for (const required of ['bin/php', 'bin/php-fpm', 'bref/etc/php/conf.d/bref.ini']) {
    if (!fs.statSync(path.join(runtime, required), { throwIfNoEntry: false })?.isFile()) {
      throw new Error(`Missing required Bref runtime file: ${required}`);
    }
  }
  for (const required of ['bin/php', 'bin/php-fpm']) {
    if (elfMachine(fs.readFileSync(path.join(runtime, required)), required, architecture) === null) {
      throw new Error(`Bref runtime binary must be a Linux 64-bit ELF executable: ${required}`);
    }
  }
  const rust = fs.readFileSync(options['rust-binary']);
  if (elfMachine(rust, 'bref-web-gateway', architecture) === null) {
    throw new Error('Rust gateway must be a Linux 64-bit ELF executable');
  }

  const archive = new Archive(architecture);
  const app = path.resolve(options['app-dir'] ?? path.join(ROOT, 'examples/minimal'));
  if (!fs.statSync(app, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(`Application staging directory does not exist: ${app}`);
  }
  archive.tree(app, '', { app: true, excluded: options.exclude.map(globToRegExp), keepDotenv: options['keep-dotenv'] });
  if (archive.entries.get(controller)?.kind !== 'file') {
    throw new Error(`Application front controller is missing or excluded: ${controller}`);
  }
  if (documentRoot !== '.' && ![...archive.entries.keys()].some((item) => item.startsWith(`${documentRoot}/`))) {
    throw new Error(`Document root is missing: ${documentRoot}`);
  }
  archive.tree(runtime, 'runtime');
  archive.add('bin/bref-web-gateway', rust, 0o755);
  for (const [source, name] of RUNTIME_FILES) archive.source(path.join(ROOT, source), name);
  const configuration = { document_root: documentRoot, front_controller: controller };
  archive.add('runtime-config.json', Buffer.from(stableJson(configuration)));
  const manifest = archive.manifest(configuration, { keep_dotenv: options['keep-dotenv'] });
  archive.add('package-manifest.json', Buffer.from(stableJson(manifest)));
  archive.validate();

  const output = path.resolve(options.output);
  archive.write(output);
  const checksum = sha256(fs.readFileSync(output));
  fs.writeFileSync(`${output}.sha256`, `${checksum}  ${path.basename(output)}\n`);
  const entries = [...archive.entries.values()];
  return {
    archive: output, sha256: checksum, architecture, files: entries.length,
    compressed_bytes: fs.statSync(output).size,
    uncompressed_bytes: entries.reduce((sum, entry) => sum + entry.data.length, 0),
    elf_files: entries.filter((entry) => entry.elfMachine !== undefined).length,
    validation: manifest.validation,
  };
}

try {
  const { values } = parseArgs({
    options: {
      'runtime-dir': { type: 'string' },
      'rust-binary': { type: 'string' },
      architecture: { type: 'string' },
      'app-dir': { type: 'string' },
      'front-controller': { type: 'string', default: 'public/index.php' },
      'document-root': { type: 'string', default: 'public' },
      exclude: { type: 'string', multiple: true, default: [] },
      'keep-dotenv': { type: 'boolean', default: false },
      output: { type: 'string' },
    },
  });
  console.log(JSON.stringify(build(values), null, 2));
} catch (error) {
  console.error(`Package build failed: ${error.message}`);
  process.exitCode = 1;
}
