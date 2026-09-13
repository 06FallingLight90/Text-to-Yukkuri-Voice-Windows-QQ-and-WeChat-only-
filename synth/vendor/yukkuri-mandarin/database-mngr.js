import { Database } from "./database.js";
import { generateHiragana, YINJIE } from "./generate-gana.js";

export class DatabaseManager {
  constructor() {
    this.db = new Database();
    this._prepopulate();
  }

  _prepopulate() {
    const entryKey = (yinjie, tone) => `${yinjie}:${tone}`;
    const seen = new Set();
    const entries = [];

    const digits = ["0", "1", "2", "3", "4", "5"];
    const middleDigits = ["1", "2", "3", "4", "5"];

    for (const yinjie of Object.keys(YINJIE)) {
      for (const d1 of digits) {
        for (const d2 of middleDigits) {
          for (const d3 of digits) {
            const tone = `${d1}${d2}${d3}`;
            const key = entryKey(yinjie, tone);
            if (!seen.has(key)) {
              seen.add(key);
              const hiragana = generateHiragana(yinjie, tone);
              entries.push([yinjie, tone, hiragana]);
            }
          }
        }
      }
    }

    this.db.insertBatch(entries);
  }

  addPinyin(yinjie, tone, hiragana) {
    if (
      tone.length !== 3 ||
      !"012345".includes(tone[0]) ||
      !"12345".includes(tone[1]) ||
      !"012345".includes(tone[2])
    ) {
      return false;
    }
    this.db.insertEntry(yinjie, tone, hiragana);
    return true;
  }

  searchByPinyin(yinjie, tone = "***") {
    if (
      tone !== "***" &&
      (tone.length !== 3 ||
        [...tone].some((t) => !"012345*".includes(t)) ||
        tone[1] === "0")
    ) {
      return [];
    }

    if (tone === "***") {
      return this.db.queryByYinjie(yinjie);
    }

    const firstDigits = tone[0] === "*" ? "012345" : [tone[0]];
    const secondDigits = tone[1] === "*" ? "12345" : [tone[1]];
    const thirdDigits = tone[2] === "*" ? "012345" : [tone[2]];

    const record = [];
    for (const d1 of firstDigits) {
      for (const d2 of secondDigits) {
        for (const d3 of thirdDigits) {
          record.push(...this.db.queryByPinyin(yinjie, `${d1}${d2}${d3}`));
        }
      }
    }
    return record;
  }

  serialSearch(serial, defaultVal = "") {
    if (!serial.length) return [];
    const result = this.db.queryBatch(serial, defaultVal);
    return result;
  }

  deletePinyin(yinjie, tone) {
    if (
      tone !== "***" &&
      (tone.length !== 3 ||
        [...tone].some((t) => !"012345*".includes(t)) ||
        tone[1] === "0")
    ) {
      return false;
    }

    if (tone === "***") {
      this.db.deleteByYinjie(yinjie);
    } else {
      const firstDigits = tone[0] === "*" ? "012345" : [tone[0]];
      const secondDigits = tone[1] === "*" ? "12345" : [tone[1]];
      const thirdDigits = tone[2] === "*" ? "012345" : [tone[2]];

      for (const d1 of firstDigits) {
        for (const d2 of secondDigits) {
          for (const d3 of thirdDigits) {
            this.db.deleteByPinyin(yinjie, `${d1}${d2}${d3}`);
          }
        }
      }
    }
    return true;
  }
}
