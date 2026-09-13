function normalizeYinjie(yinjie) {
  return yinjie.replace(/ü/g, "v").replace(/u:/g, "v");
}

export class Database {
  constructor() {
    this._data = new Map();
  }

  insertEntry(yinjie, tone, hiragana) {
    const key = `${normalizeYinjie(yinjie)}:${tone}`;
    this._data.set(key, { yinjie: normalizeYinjie(yinjie), tone, hiragana });
  }

  insertBatch(entries) {
    for (const [yinjie, tone, hiragana] of entries) {
      this.insertEntry(yinjie, tone, hiragana);
    }
  }

  queryByPinyin(yinjie, tone) {
    const key = `${normalizeYinjie(yinjie)}:${tone}`;
    const entry = this._data.get(key);
    return entry ? [[entry.yinjie, entry.tone, entry.hiragana]] : [];
  }

  queryByYinjie(yinjie) {
    const norm = normalizeYinjie(yinjie);
    const results = [];
    for (const [, entry] of this._data) {
      if (entry.yinjie === norm) {
        results.push([entry.yinjie, entry.tone, entry.hiragana]);
      }
    }
    return results;
  }

  queryBatch(entries, defaultVal) {
    return entries.map(([yinjie, tone]) => {
      const key = `${normalizeYinjie(yinjie)}:${tone}`;
      const entry = this._data.get(key);
      if (entry) return entry.hiragana;
      if (defaultVal === "keep") return yinjie;
      return defaultVal;
    });
  }

  queryAll() {
    const results = [];
    for (const [, entry] of this._data) {
      results.push([entry.yinjie, entry.tone, entry.hiragana]);
    }
    return results;
  }

  deleteByPinyin(yinjie, tone) {
    const key = `${normalizeYinjie(yinjie)}:${tone}`;
    this._data.delete(key);
  }

  deleteByYinjie(yinjie) {
    const norm = normalizeYinjie(yinjie);
    for (const [key, entry] of this._data) {
      if (entry.yinjie === norm) {
        this._data.delete(key);
      }
    }
  }

  close() {}
}
