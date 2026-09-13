import { normalizeGana } from "./non-hanzi-process.js";

export function postProcess(sentence, withoutAccent) {
  if (!withoutAccent) return sentence;

  const accentSymbols = new Set(["'", "/", "_"]);
  const result = [];
  for (const char of sentence) {
    if (!accentSymbols.has(char)) {
      result.push(char);
    }
  }
  return normalizeGana(result.join(""));
}
