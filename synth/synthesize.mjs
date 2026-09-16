#!/usr/bin/env node
/**
 * YouKuLiChaSpeak — offline yukkuri speech synthesis sidecar.
 *
 * Turns text into an AquesTalk "yukkuri" WAV file, fully offline:
 *
 *   Chinese  : text (hanzi) -> pinyin -> kana + pitch marks -> AquesTalk
 *              (via the vendored MIT `yukkuri-mandarin` converter)
 *   Japanese : text (kanji/kana) -> AquesTalk phonetic notation -> AquesTalk
 *              (via the MIT `kanji2koe-openjtalk` reimplementation of AqKanji2Koe)
 *   Raw      : text is already AquesTalk phonetic notation, passed through as-is
 *
 * Two modes:
 *   one-shot : node synthesize.mjs --text "..." --lang zh --out out.wav
 *   serve    : node synthesize.mjs --serve      (line-delimited JSON on stdio)
 *
 * SPDX-License-Identifier: MIT
 */

import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";

import { load as loadAquesTalk } from "aquestalk.js";
import { load as loadKanji2Koe } from "kanji2koe-openjtalk";
import { textConvert } from "./vendor/yukkuri-mandarin/index.js";

/** Voice banks shipped with aquestalk.js. f1 = Reimu, f2 = Marisa. */
export const VOICES = ["f1", "f2", "m1", "m2", "dvd", "imd1", "jgr", "r1"];

/** Voices that are the classic yukkuri pair. */
export const YUKKURI_VOICES = ["f1", "f2"];

const DEFAULT_VOICE = "f1";
const DEFAULT_SPEED = 100;
/** Minimum allowed by AquesTalk; below ~50 it gets unintelligible. */
const MIN_SPEED = 50;
const MAX_SPEED = 300;

/**
 * Chinese only: skip the Japanese pitch accents that the front-end derives from
 * Mandarin tones.
 *
 * The pinyin -> kana step maps each syllable's tone onto a *Japanese* pitch
 * accent, and the resulting contour falls where a Mandarin speaker does not
 * expect it. Dropping those marks leaves the kana themselves byte-identical -
 * only the pitch contour goes away - which reads as noticeably more natural.
 * Japanese input is unaffected: its pitch accents come from the dictionary and
 * are what makes it sound like Japanese at all.
 */
const DEFAULT_WITHOUT_ACCENT = true;

/**
 * v86 memory ceiling. The emulated AquesTalk PE needs well under this; keeping
 * it modest avoids a multi-hundred-megabyte allocation per engine instance.
 */
const EMU_MEMORY_BYTES = 128 * 1024 * 1024;

/** Log lines must never pollute stdout in serve mode, so everything goes to stderr. */
function log(...args) {
  process.stderr.write(`[synth] ${args.join(" ")}\n`);
}

/* ------------------------------------------------------------------ WAV --- */

/**
 * Read just enough of a RIFF/WAVE header to learn the format and duration.
 * @param {Buffer} buffer
 */
export function inspectWav(buffer) {
  if (buffer.length < 12 || buffer.toString("ascii", 0, 4) !== "RIFF") {
    throw new Error("not a RIFF file");
  }
  if (buffer.toString("ascii", 8, 12) !== "WAVE") {
    throw new Error("not a WAVE file");
  }

  let offset = 12;
  /** @type {{sampleRate:number, channels:number, bitsPerSample:number}|null} */
  let fmt = null;
  let dataBytes = 0;

  while (offset + 8 <= buffer.length) {
    const id = buffer.toString("ascii", offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const body = offset + 8;
    if (id === "fmt " && body + 16 <= buffer.length) {
      fmt = {
        channels: buffer.readUInt16LE(body + 2),
        sampleRate: buffer.readUInt32LE(body + 4),
        bitsPerSample: buffer.readUInt16LE(body + 14),
      };
    } else if (id === "data") {
      dataBytes = Math.min(size, buffer.length - body);
      break;
    }
    offset = body + size + (size % 2);
  }

  if (!fmt) throw new Error("WAVE file has no fmt chunk");
  const bytesPerFrame = (fmt.bitsPerSample / 8) * fmt.channels;
  const frames = bytesPerFrame > 0 ? dataBytes / bytesPerFrame : 0;
  return {
    ...fmt,
    dataBytes,
    durationSec: fmt.sampleRate > 0 ? frames / fmt.sampleRate : 0,
  };
}

/* --------------------------------------------------------------- engine --- */

export class YukkuriSynth {
  constructor() {
    /** @type {import("aquestalk.js").AquesTalk|null} */
    this.aq = null;
    this.voice = null;
    /** @type {Awaited<ReturnType<typeof loadKanji2Koe>>|null} */
    this.kanji2koe = null;
    /** Serialises engine access: v86 emulators are not reentrant. */
    this.chain = Promise.resolve();
  }

  /** Run `task` after every previously queued task has settled. */
  #exclusive(task) {
    const run = this.chain.then(task, task);
    // Keep the chain alive even when a task rejects.
    this.chain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  async ensureVoice(voice) {
    if (this.aq && this.voice === voice) return;
    if (this.aq) {
      try {
        await this.aq.destroy();
      } catch {
        /* the emulator is being torn down anyway */
      }
      this.aq = null;
      this.voice = null;
    }
    log(`loading voice ${voice}`);
    this.aq = await loadAquesTalk(voice, { memorySize: EMU_MEMORY_BYTES });
    this.voice = voice;
  }

  async ensureKanji2Koe() {
    if (this.kanji2koe) return this.kanji2koe;
    log("loading japanese converter (OpenJTalk + NAIST-JDic)");
    this.kanji2koe = await loadKanji2Koe();
    return this.kanji2koe;
  }

  /**
   * Convert input text into an AquesTalk phonetic notation string.
   * @param {string} text
   * @param {"zh"|"ja"|"raw"} lang
   * @param {{withoutAccent?: boolean}} [options] Chinese only; see
   *   DEFAULT_WITHOUT_ACCENT. Ignored for "ja" and "raw".
   */
  async toNotation(text, lang, options = {}) {
    if (lang === "raw") return text;
    if (lang === "zh") {
      const withoutAccent = options.withoutAccent ?? DEFAULT_WITHOUT_ACCENT;
      return textConvert(text, { withoutAccent });
    }
    if (lang === "ja") {
      const converter = await this.ensureKanji2Koe();
      return converter.convert(text);
    }
    throw new Error(`unsupported lang: ${lang}`);
  }

  /**
   * @param {{text:string, lang?:"zh"|"ja"|"raw", voice?:string, speed?:number,
   *   withoutAccent?:boolean}} request
   * @returns {Promise<{wav:Buffer, notation:string, durationSec:number, voice:string, speed:number}>}
   */
  async synthesize(request) {
    const text = String(request.text ?? "");
    if (!text.trim()) throw new Error("text is empty");

    const lang = request.lang ?? "zh";
    const voice = request.voice ?? DEFAULT_VOICE;
    if (!VOICES.includes(voice)) {
      throw new Error(`unknown voice ${voice}; expected one of ${VOICES.join(", ")}`);
    }
    const speed = Number.isFinite(request.speed) ? Number(request.speed) : DEFAULT_SPEED;
    if (speed < MIN_SPEED || speed > MAX_SPEED) {
      throw new Error(`speed ${speed} is out of range ${MIN_SPEED}..${MAX_SPEED}`);
    }

    const notation = await this.toNotation(text, lang, {
      withoutAccent: request.withoutAccent,
    });
    if (!notation || !notation.trim()) {
      throw new Error("conversion produced empty phonetic notation");
    }

    return this.#exclusive(async () => {
      await this.ensureVoice(voice);
      const wav = this.aq.run(notation, Math.round(speed));
      const buffer = Buffer.from(wav.buffer ?? wav, wav.byteOffset ?? 0, wav.byteLength);
      const info = inspectWav(buffer);
      return {
        wav: buffer,
        notation,
        durationSec: info.durationSec,
        voice,
        speed: Math.round(speed),
      };
    });
  }

  async dispose() {
    if (this.aq) {
      try {
        await this.aq.destroy();
      } catch {
        /* ignore */
      }
      this.aq = null;
      this.voice = null;
    }
  }
}

/* ------------------------------------------------------------------- CLI --- */

function parseArgs(argv) {
  /** @type {Record<string, string|boolean>} */
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      i++;
    }
  }
  return out;
}

const HELP = `YouKuLiChaSpeak offline yukkuri synthesizer

Usage:
  node synthesize.mjs --text "你好世界" --out hello.wav [options]
  node synthesize.mjs --serve
  node synthesize.mjs --check

Options:
  --text <string>     text to speak
  --lang <zh|ja|raw>  input language (default: zh)
  --voice <name>      ${VOICES.join(", ")} (default: ${DEFAULT_VOICE})
  --speed <int>       ${MIN_SPEED}..${MAX_SPEED} (default: ${DEFAULT_SPEED})
  --without-accent    Chinese: no Japanese pitch accents (default)
  --accent            Chinese: keep the pitch accents derived from the tones
  --out <file>        output .wav path
  --notation-only     print the phonetic notation instead of synthesizing
  --serve             stay resident; read JSON requests on stdin, reply on stdout
  --check             boot the engine, synthesize a sample, and exit
  --help              show this message

Serve mode protocol (one JSON object per line on stdin):
  {"id":1,"text":"你好","lang":"zh","voice":"f1","speed":100,"out":"C:\\\\tmp\\\\a.wav"}

Reply (one JSON object per line on stdout):
  {"id":1,"ok":true,"out":"...","durationSec":1.23,"notation":"...","voice":"f1"}
  {"id":1,"ok":false,"error":"..."}
`;

/**
 * Chinese pitch accents, from the CLI flags: `--without-accent` drops them,
 * `--accent` keeps them, neither takes DEFAULT_WITHOUT_ACCENT.
 */
function accentOption(args) {
  if (args["without-accent"]) return true;
  if (args.accent) return false;
  return DEFAULT_WITHOUT_ACCENT;
}

async function runOnce(args) {
  const synth = new YukkuriSynth();
  try {
    const text = typeof args.text === "string" ? args.text : "";
    if (!text) throw new Error("--text is required");

    if (args["notation-only"]) {
      const notation = await synth.toNotation(text, String(args.lang ?? "zh"), {
        withoutAccent: accentOption(args),
      });
      process.stdout.write(`${notation}\n`);
      return 0;
    }

    const out = typeof args.out === "string" ? path.resolve(args.out) : "";
    if (!out) throw new Error("--out is required");

    const result = await synth.synthesize({
      text,
      lang: String(args.lang ?? "zh"),
      voice: String(args.voice ?? DEFAULT_VOICE),
      speed: args.speed === undefined ? DEFAULT_SPEED : Number(args.speed),
      withoutAccent: accentOption(args),
    });

    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, result.wav);
    log(
      `wrote ${out} (${result.durationSec.toFixed(2)}s, ${result.wav.length} bytes, ` +
        `voice=${result.voice}, speed=${result.speed})`,
    );
    log(`notation: ${result.notation}`);
    return 0;
  } finally {
    await synth.dispose();
  }
}

async function runCheck() {
  const synth = new YukkuriSynth();
  const cases = [
    { lang: "zh", text: "你好，世界", voice: "f1" },
    { lang: "zh", text: "油库里普通话测试", voice: "f2" },
    { lang: "ja", text: "ゆっくりしていってね", voice: "f1" },
    { lang: "ja", text: "日本語のテストです。", voice: "f2" },
    { lang: "raw", text: "ゆっくり/して'いってね", voice: "m1" },
  ];
  let failures = 0;
  try {
    for (const item of cases) {
      const started = Date.now();
      try {
        const result = await synth.synthesize({ ...item, speed: 100 });
        process.stdout.write(
          `OK   ${item.lang.padEnd(3)} ${item.voice.padEnd(4)} ` +
            `${result.durationSec.toFixed(2)}s  ${(Date.now() - started) / 1000}s  ` +
            `"${item.text}" -> ${result.notation}\n`,
        );
      } catch (error) {
        failures++;
        process.stdout.write(`FAIL ${item.lang} ${item.voice} "${item.text}": ${error.message}\n`);
      }
    }
  } finally {
    await synth.dispose();
  }
  return failures === 0 ? 0 : 1;
}

async function runServe() {
  const synth = new YukkuriSynth();
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

  const reply = (payload) => {
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  };

  // If the parent process goes away, stop instead of lingering as an orphan.
  process.stdout.on("error", () => process.exit(0));

  // Warm the default voice so the first real request is not slow.
  try {
    await synth.ensureVoice(DEFAULT_VOICE);
    reply({ id: 0, ok: true, event: "ready", voices: VOICES });
  } catch (error) {
    reply({ id: 0, ok: false, event: "ready", error: String(error?.message ?? error) });
  }

  try {
    for await (const line of rl) {
      const trimmed = line.trim();
      if (!trimmed) continue;

      let request;
      try {
        request = JSON.parse(trimmed);
      } catch (error) {
        reply({ id: null, ok: false, error: `invalid JSON: ${error.message}` });
        continue;
      }

      if (request.action === "shutdown") {
        reply({ id: request.id ?? null, ok: true, event: "shutdown" });
        break;
      }

      try {
        const result = await synth.synthesize(request);
        const out = typeof request.out === "string" ? path.resolve(request.out) : "";
        if (out) {
          fs.mkdirSync(path.dirname(out), { recursive: true });
          fs.writeFileSync(out, result.wav);
        }
        reply({
          id: request.id ?? null,
          ok: true,
          out,
          durationSec: result.durationSec,
          notation: result.notation,
          voice: result.voice,
          speed: result.speed,
          bytes: result.wav.length,
        });
      } catch (error) {
        reply({ id: request.id ?? null, ok: false, error: String(error?.message ?? error) });
      }
    }
  } finally {
    // Release stdin, otherwise the open pipe keeps the event loop alive and the
    // sidecar outlives the GUI that spawned it.
    rl.close();
    process.stdin.pause();
    if (typeof process.stdin.destroy === "function") process.stdin.destroy();
    await synth.dispose();
  }

  return 0;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(HELP);
    return 0;
  }
  if (args.serve) return runServe();
  if (args.check) return runCheck();
  return runOnce(args);
}

const invokedDirectly =
  process.argv[1] && import.meta.url === new URL(`file://${process.argv[1].replace(/\\/g, "/")}`).href;

if (invokedDirectly || process.argv[1]?.endsWith("synthesize.mjs")) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      log(`fatal: ${error?.stack ?? error}`);
      process.exitCode = 1;
    });
}
