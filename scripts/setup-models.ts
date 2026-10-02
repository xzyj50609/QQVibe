// Written for this project (not vendored).
//
// One-time model setup: downloads the pinned Laya multilingual ONNX bundle into `.models/laya`
// with streaming writes to `<file>.part`, then verifies each file's SHA256 and byte size before
// renaming it into place. This is the ONLY place the analyzer is allowed to use the network;
// inference and OCR are offline.
//
// Usage (once npm scripts exist):
//   npm run setup:models            # download missing / stale files
//   npm run setup:models -- --force # re-download everything
//   npm run setup:models -- --dir D:\models\laya
//
// The pins below match the frozen contract. Do not change them without also updating
// electron/analysis.ts's MODEL_REVISION and re-running scripts/test-model.ts.

import { createHash } from "node:crypto";
import {
  createReadStream,
  createWriteStream,
  existsSync,
  mkdirSync,
  renameSync,
  unlinkSync,
  statSync,
} from "node:fs";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const MODEL_REPO = "mizchi/laya-multilingual-onnx";
export const MODEL_REVISION = "d9d003d543e63d6d3375c21d44624136bd1e0bad";

export interface PinnedFile {
  /** Path relative to the model directory. */
  path: string;
  bytes: number;
  sha256: string;
}

/**
 * Fixed bundle files and their hashes. model.onnx and tokenizer/tokenizer.json hashes are given
 * by the contract; the small JSON files were pinned by querying the Hugging Face metadata for
 * revision d9d003d543e63d6d3375c21d44624136bd1e0bad.
 */
export const PINNED_FILES: PinnedFile[] = [
  {
    path: "model.onnx",
    bytes: 646870871,
    sha256: "0b095e005a4c295cae74d47b7eb6931c369d48f5b720b45278d774165798310c",
  },
  {
    path: "tokenizer/tokenizer.json",
    bytes: 34363188,
    sha256: "609d8f4c067cd3950f88594c5a802616cea245823836ef5848ee4fc40aab5b6f",
  },
  {
    path: "rl_agent_config.json",
    bytes: 473,
    sha256: "9a669a70961064c3c6cc76d2afb8bc5fb10dcd8349bb66e5f7b9b1afb74440d5",
  },
  {
    path: "onnx_config.json",
    bytes: 332,
    sha256: "13db475255d076da580a3435f28904e3360fe7f6380d7e3c75ee586e966ca5f0",
  },
  {
    path: "README.md",
    bytes: 2743,
    sha256: "cc4deb231c7398076c31cef3583785c9fefeb8d154fe386d733bee19e9a969ff",
  },
  {
    path: "tokenizer/tokenizer_config.json",
    bytes: 524,
    sha256: "6c6b2d8e3c84ce0e671c129cd6b374b235d6f9863042a5836358d00a89bbb5a1",
  },
];

export const DEFAULT_MODEL_DIR = path.resolve(process.cwd(), ".models", "laya");

interface Options {
  dir: string;
  force: boolean;
}

function parseArgs(argv: string[]): Options {
  let dir = process.env.LAYA_MODEL_DIR ?? DEFAULT_MODEL_DIR;
  let force = false;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--force" || arg === "-f") {
      force = true;
    } else if (arg === "--dir") {
      const value = argv[++i];
      if (!value) throw new Error("--dir requires a path");
      dir = path.resolve(value);
    } else if (arg === "--help" || arg === "-h") {
      console.log(
        [
          "Usage: setup-models [--force] [--dir <path>]",
          "",
          `Default directory: ${DEFAULT_MODEL_DIR}`,
          "Environment: LAYA_MODEL_DIR overrides the default directory.",
        ].join("\n"),
      );
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return { dir, force };
}

async function hashFile(file: string): Promise<{ sha256: string; bytes: number }> {
  const hash = createHash("sha256");
  let bytes = 0;
  for await (const chunk of createReadStream(file)) {
    const buffer = chunk as Buffer;
    hash.update(buffer);
    bytes += buffer.length;
  }
  return { sha256: hash.digest("hex"), bytes };
}

function formatBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(1)} KB`;
  return `${bytes} B`;
}

function removePartial(part: string): void {
  // This is always one file. unlinkSync also handles Unicode paths on the pinned
  // Windows Node runtime, where rmSync can leave the partial file behind.
  try {
    unlinkSync(part);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}

export async function downloadFile(
  spec: PinnedFile, options: Options, fetchImpl: typeof fetch = fetch,
  resetPartial = options.force,
): Promise<void> {
  const dest = path.join(options.dir, spec.path);
  const part = `${dest}.part`;
  const url = `https://huggingface.co/${MODEL_REPO}/resolve/${MODEL_REVISION}/${spec.path}`;

  if (!options.force && existsSync(dest)) {
    if (statSync(dest).size === spec.bytes && (await hashFile(dest)).sha256 === spec.sha256) {
      console.log(`[ok]   ${spec.path} (已存在，SHA256 校验通过)`);
      return;
    }
    console.log(`[warn] ${spec.path} 校验不匹配，重新下载`);
  }
  mkdirSync(path.dirname(dest), { recursive: true });
  if (resetPartial) removePartial(part);

  let offset = 0;
  if (existsSync(part)) {
    const size = statSync(part).size;
    if (size === spec.bytes && (await hashFile(part)).sha256 === spec.sha256) {
      renameSync(part, dest);
      return;
    }
    if (size > 0 && size < spec.bytes) offset = size;
    else removePartial(part);
  }
  const headers = offset > 0 ? { Range: `bytes=${offset}-` } : undefined;
  console.log(`[get]  ${spec.path}${offset ? ` (续传 ${formatBytes(offset)})` : ""}`);
  let response = await fetchImpl(url, {
    redirect: "follow", headers, signal: AbortSignal.timeout(600_000),
  });
  if (offset > 0 && response.status === 416) {
    await response.body?.cancel();
    offset = 0;
    removePartial(part);
    response = await fetchImpl(url, { redirect: "follow", signal: AbortSignal.timeout(600_000) });
  }
  if (!response.ok || !response.body) {
    await response.body?.cancel();
    throw new Error(`下载 ${spec.path} 失败: HTTP ${response.status} ${response.statusText}`);
  }
  if (response.status === 206) {
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get("content-range") ?? "");
    if (!match || Number(match[1]) !== offset || Number(match[3]) !== spec.bytes ||
        Number(match[2]) < offset || Number(match[2]) >= spec.bytes) {
      await response.body.cancel();
      throw new Error(`下载 ${spec.path} 失败: Content-Range 不匹配`);
    }
  } else if (response.status === 200) {
    // A server may ignore Range and send the full file. Never append that response.
    offset = 0;
  } else {
    await response.body.cancel();
    throw new Error(`下载 ${spec.path} 失败: unexpected HTTP ${response.status}`);
  }

  const hash = createHash("sha256");
  let received = offset;
  if (offset > 0) for await (const chunk of createReadStream(part)) hash.update(chunk as Buffer);
  let lastReported = offset;
  const hasher = new Transform({
    transform(chunk: Buffer, _encoding, callback) {
      received += chunk.length;
      if (received > spec.bytes) {
        callback(new Error(`下载 ${spec.path} 超过期望大小`));
        return;
      }
      hash.update(chunk);
      if (received - lastReported >= 32 * 1024 * 1024 || received === spec.bytes) {
        lastReported = received;
        process.stdout.write(`\r       ${formatBytes(received)} / ${formatBytes(spec.bytes)}`);
      }
      callback(null, chunk);
    },
  });
  try {
    await pipeline(
      Readable.fromWeb(response.body as unknown as import("node:stream/web").ReadableStream),
      hasher, createWriteStream(part, { flags: offset > 0 ? "a" : "w" }),
    );
  } catch (error) {
    if (received > spec.bytes) removePartial(part);
    throw error;
  }
  if (received !== spec.bytes) {
    throw new Error(`下载 ${spec.path} 被截断: 收到 ${received} 字节，期望 ${spec.bytes} 字节`);
  }
  if (hash.digest("hex") !== spec.sha256) {
    removePartial(part);
    throw new Error(`下载 ${spec.path} SHA256 校验失败`);
  }
  // Only replace an existing model after the new file has passed every check.
  renameSync(part, dest);
  console.log(`\n[done] ${spec.path} (${formatBytes(spec.bytes)})`);
}

export async function downloadWithRetries(
  spec: PinnedFile, options: Options, fetchImpl: typeof fetch = fetch,
  wait: (ms: number) => Promise<void> = (ms) => new Promise(resolve => setTimeout(resolve, ms)),
): Promise<void> {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await downloadFile(spec, options, fetchImpl, options.force && attempt === 1);
      return;
    } catch (error) {
      if (attempt === 3) throw error;
      console.warn(`[retry] ${spec.path} 第 ${attempt} 次失败，将重试（有效片段保留）`);
      await wait(attempt * 1500);
    }
  }
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));
  console.log(`Laya 模型目录: ${options.dir}`);
  console.log(`仓库: ${MODEL_REPO}@${MODEL_REVISION}\n`);
  mkdirSync(options.dir, { recursive: true });
  for (const spec of PINNED_FILES) await downloadWithRetries(spec, options);
  console.log("\n全部模型文件已就绪并校验通过。");
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`\n[error] ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  });
}
