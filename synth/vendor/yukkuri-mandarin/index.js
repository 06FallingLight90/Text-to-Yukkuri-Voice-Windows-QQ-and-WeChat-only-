import { textConvert, pinyinConvert } from "./core.js";
import { DatabaseManager } from "./database-mngr.js";
import { NonHanziModes } from "./settings.js";
import { cleanPunctuation, normalizeGana, isSupported, filterUnsupported } from "./non-hanzi-process.js";

export { textConvert, pinyinConvert };
export { DatabaseManager };
export { NonHanziModes };
export { cleanPunctuation, normalizeGana, isSupported, filterUnsupported };

export default {
  textConvert,
  pinyinConvert,
  DatabaseManager,
  NonHanziModes,
  cleanPunctuation,
  normalizeGana,
};
