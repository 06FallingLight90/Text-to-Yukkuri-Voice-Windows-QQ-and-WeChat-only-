import { preProcess } from "./pre-process.js";
import { hanziProcess } from "./hanzi-process.js";
import { nonHanziProcess, filterUnsupported } from "./non-hanzi-process.js";
import { postProcess } from "./post-process.js";
import { NonHanziModes } from "./settings.js";
import { DatabaseManager } from "./database-mngr.js";

export function textConvert(
  sentence,
  {
    withoutAccent = false,
    tokenizer = null,
    pinyinDatabase = null,
    nonHanziConfig = null,
    longPause = false,
  } = {}
) {
  if (typeof sentence !== "string") {
    throw new Error(`参数sentence必须是字符串: ${sentence}`);
  }
  if (!sentence) return "";

  sentence = filterUnsupported(sentence);
  sentence = preProcess(sentence);
  const [hanzi, nonHanzi, lastType] = divide(sentence);
  const resHanzi = hanziProcess(hanzi, tokenizer, pinyinDatabase);
  const resNonHanzi = nonHanziProcess(nonHanzi, nonHanziConfig, { longPause });
  let result = combine(resHanzi, resNonHanzi, lastType);
  result = postProcess(result, withoutAccent);
  return result;
}

export function divide(sentence) {
  if (!sentence) return [[], [], false];

  const hanzi = [];
  const nonHanzi = [];
  let wordBasket = [];
  let basketType = isHanzi(sentence[0]);

  for (const char of sentence) {
    const currentType = isHanzi(char);
    if (currentType === basketType) {
      wordBasket.push(char);
    } else {
      if (basketType) {
        hanzi.push(wordBasket.join(""));
      } else {
        nonHanzi.push(wordBasket.join(""));
      }
      wordBasket = [char];
      basketType = currentType;
    }
  }

  if (basketType) {
    hanzi.push(wordBasket.join(""));
  } else {
    nonHanzi.push(wordBasket.join(""));
  }

  return [hanzi, nonHanzi, basketType];
}

export function combine(resHanzi, resNonHanzi, lastType) {
  if (!resHanzi.length && !resNonHanzi.length) return "";

  const lenHanzi = resHanzi.length;
  const lenNonHanzi = resNonHanzi.length;
  const result = [];

  if (lenHanzi === lenNonHanzi) {
    if (lastType) {
      for (let i = 0; i < lenHanzi; i++) {
        result.push(resNonHanzi[i]);
        result.push(resHanzi[i]);
      }
    } else {
      for (let i = 0; i < lenHanzi; i++) {
        result.push(resHanzi[i]);
        result.push(resNonHanzi[i]);
      }
    }
  } else if (lenHanzi - lenNonHanzi === 1) {
    for (let i = 0; i < lenNonHanzi; i++) {
      result.push(resHanzi[i]);
      result.push(resNonHanzi[i]);
    }
    result.push(resHanzi.at(-1));
  } else if (lenNonHanzi - lenHanzi === 1) {
    for (let i = 0; i < lenHanzi; i++) {
      result.push(resNonHanzi[i]);
      result.push(resHanzi[i]);
    }
    result.push(resNonHanzi.at(-1));
  } else {
    throw new Error(
      `转换过程出错: hanzi片段数(${lenHanzi})与nonHanzi片段数(${lenNonHanzi})不匹配`
    );
  }

  return result.join("");
}

export function isHanzi(fragment) {
  if (!fragment) return false;
  for (const char of fragment) {
    const code = char.charCodeAt(0);
    if (!(0x4e00 <= code && code <= 0x9fff)) return false;
  }
  return true;
}

export function pinyinConvert(
  sentence,
  { error = "", withoutAccent = false, pinyinDatabase = null } = {}
) {
  if (typeof sentence !== "string") {
    throw new Error(`参数sentence必须是字符串: ${sentence}`);
  }
  if (!sentence) return "";

  const mark = "/0";
  const pinyinList = [mark, ...sentence.split(/\s+/), mark];

  for (let i = 0; i < pinyinList.length; i++) {
    if (!"012345".includes(pinyinList[i].at(-1))) {
      pinyinList[i] = `${pinyinList[i]}0`;
    }
  }

  const serial = [];
  for (let i = 1; i < pinyinList.length - 1; i++) {
    serial.push([
      pinyinList[i].slice(0, -1),
      `${pinyinList[i - 1].at(-1)}${pinyinList[i].at(-1)}${pinyinList[i + 1].at(-1)}`,
    ]);
  }

  if (!pinyinDatabase) {
    pinyinDatabase = new DatabaseManager();
  }
  const hiraganaList = pinyinDatabase.serialSearch(serial, error);

  const punctuationMap = {
    ",": "、",
    ".": "。",
    ";": ",",
    "?": "?",
    ":": "、",
    "!": "。",
    "-": "、",
    "~": "、",
  };
  const resultList = [];
  for (let i = 0; i < serial.length; i++) {
    if (serial[i][1][1] === "0") {
      resultList.push(punctuationMap[serial[i][0]] ?? error);
    } else {
      resultList.push(hiraganaList[i]);
    }
  }

  let result = resultList.join("");
  result = postProcess(result, withoutAccent);
  return result;
}
