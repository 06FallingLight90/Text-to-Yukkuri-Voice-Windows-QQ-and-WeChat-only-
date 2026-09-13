import { pinyin } from "pinyin-pro";
import { NonHanziModes } from "./settings.js";

const _pinyinCache = new Map();

function hasPinyin(char) {
  let result = _pinyinCache.get(char);
  if (result === undefined) {
    const p = pinyin(char, { type: "array", toneType: "num", v: true });
    result = p.length > 0 && /^[a-z]+[0-5]$/.test(p[0]);
    _pinyinCache.set(char, result);
  }
  return result;
}

export function nonHanziProcess(fragments, config = null, { longPause = false } = {}) {
  if (!fragments || !fragments.length) return [];

  if (!config) {
    config = new NonHanziModes({
      pcMode: (fragment) => cleanPunctuation(fragment, longPause),
      jaMode: normalizeGana,
    });
  }

  const result = [];
  for (const fragment of fragments) {
    if (!fragment) {
      result.push("");
      continue;
    }
    const processedFragment = [];
    let charBasket = [fragment[0]];
    let basketFlag = classify(fragment[0]);

    for (const char of fragment.slice(1)) {
      const currentFlag = classify(char);
      if (currentFlag === basketFlag) {
        charBasket.push(char);
      } else {
        processedFragment.push(
          convertorHandler(charBasket.join(""), basketFlag, config)
        );
        charBasket = [char];
        basketFlag = currentFlag;
      }
    }
    processedFragment.push(
      convertorHandler(charBasket.join(""), basketFlag, config)
    );
    result.push(processedFragment.join(""));
  }
  return result;
}

function toHalfWidth(char) {
  const code = char.charCodeAt(0);
  if (code >= 0xff01 && code <= 0xff5e) {
    return String.fromCharCode(code - 0xfee0);
  }
  return char;
}

function isSupported(char) {
  if (!char || char.length !== 1) return false;
  const code = char.charCodeAt(0);
  if (0x4e00 <= code && code <= 0x9fff) return hasPinyin(char);
  if (char >= "0" && char <= "9") return true;
  const normalized = toHalfWidth(char);
  return classify(normalized) !== "others";
}

function filterUnsupported(text) {
  if (!text) return "";
  return [...text].filter((c) => isSupported(c)).join("");
}

function classify(char) {
  if (char.length !== 1) {
    throw new Error("输入必须是单个字符。");
  }
  const code = char.charCodeAt(0);
  if (
    (0x3040 <= code && code <= 0x309f) ||
    (0x30a0 <= code && code <= 0x30ff) ||
    (0xff65 <= code && code <= 0xff9f)
  ) {
    return "gana";
  }
  if (
    (0x0041 <= code && code <= 0x005a) ||
    (0x0061 <= code && code <= 0x007a)
  ) {
    return "latin";
  }

  if (char === " " || char === "…" || char === "—" || char === "·") {
    return "punctuation";
  }

  const punctuation = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
  if (punctuation.includes(char)) return "punctuation";

  if (
    (code >= 0x3000 && code <= 0x303f) ||
    (code >= 0xfe30 && code <= 0xfe4f) ||
    (code >= 0xfe10 && code <= 0xfe19) ||
    (code >= 0xff01 && code <= 0xff0f) ||
    (code >= 0xff1a && code <= 0xff20) ||
    (code >= 0xff3b && code <= 0xff40) ||
    (code >= 0xff5b && code <= 0xff65) ||
    code === 0x3003 ||
    code === 0x3005
  ) {
    return "punctuation";
  }
  return "others";
}

function modeHandler(char, mode, replace = "") {
  if (mode === "ignore") return "";
  if (mode === "keep") return char;
  if (mode === "replace") return replace;
  if (typeof mode === "function") return mode(char);
  throw new Error("参数mode错误！请查看说明。");
}

function convertorHandler(fragment, type, config) {
  if (type === "punctuation")
    return punctuationConvert(fragment, config.pcMode, config.pcReplace);
  if (type === "gana")
    return ganaConvert(fragment, config.jaMode, config.jaReplace);
  if (type === "latin")
    return latinConvert(fragment, config.enMode, config.enReplace);
  if (type === "others")
    return othersConvert(fragment, config.otherMode, config.otherReplace);
  throw new Error("参数type错误，请查看说明。");
}

function punctuationConvert(fragment, mode, replace) {
  return modeHandler(fragment, mode, replace);
}

function cleanPunctuation(fragment, longPause = false) {
  if (!fragment) return "";
  const normalStop = new Set([
    ",", "，", "、", ";", "；", ":", "：", "~", "-", "—", "…", "－", "·", " ",
  ]);
  const fullStop = new Set([".", "｡", "。", "!", "！"]);
  const weakStop = new Set([
    "(", ")", "（", "）", "[", "]", "【", "】", "「", "」", "『", "』",
  ]);
  const questionMark = new Set(["?", "？"]);

  const result = [];
  for (const char of fragment) {
    if (normalStop.has(char)) result.push("、");
    else if (fullStop.has(char)) result.push(longPause ? "。。" : "。");
    else if (weakStop.has(char)) result.push(",");
    else if (questionMark.has(char)) result.push("?");
  }
  return result.join("");
}

function ganaConvert(fragment, mode, replace) {
  return modeHandler(fragment, mode, replace);
}

function normalizeGana(fragment) {
  if (!fragment) return "";

  const fullWidthKatakana = [
    "ア", "イ", "ウ", "エ", "オ", "カ", "キ", "ク", "ケ", "コ",
    "サ", "シ", "ス", "セ", "ソ", "タ", "チ", "ツ", "テ", "ト",
    "ナ", "ニ", "ヌ", "ネ", "ノ", "ハ", "ヒ", "フ", "ヘ", "ホ",
    "マ", "ミ", "ム", "メ", "モ", "ヤ", "ユ", "ヨ", "ラ", "リ",
    "ル", "レ", "ロ", "ワ", "ヲ", "ン", "ガ", "ギ", "グ", "ゲ",
    "ゴ", "ザ", "ジ", "ズ", "ゼ", "ゾ", "ダ", "ヂ", "ヅ", "デ",
    "ド", "バ", "ビ", "ブ", "ベ", "ボ", "パ", "ピ", "プ", "ペ",
    "ポ", "ー", "ァ", "ィ", "ゥ", "ェ", "ォ", "ャ", "ュ", "ョ",
    "ッ", "ヴ", "ヰ", "ヱ",
  ];

  const halfWidthKatakana = [
    "ｱ", "ｲ", "ｳ", "ｴ", "ｵ", "ｶ", "ｷ", "ｸ", "ｹ", "ｺ", "ｻ",
    "ｼ", "ｽ", "ｾ", "ｿ", "ﾀ", "ﾁ", "ﾂ", "ﾃ", "ﾄ", "ﾅ", "ﾆ",
    "ﾇ", "ﾈ", "ﾉ", "ﾊ", "ﾋ", "ﾌ", "ﾍ", "ﾎ", "ﾏ", "ﾐ", "ﾑ",
    "ﾒ", "ﾓ", "ﾔ", "ﾕ", "ﾖ", "ﾗ", "ﾘ", "ﾙ", "ﾚ", "ﾛ", "ﾜ",
    "ｦ", "ﾝ", "ｶﾞ", "ｷﾞ", "ｸﾞ", "ｹﾞ", "ｺﾞ", "ｻﾞ", "ｼﾞ", "ｽﾞ",
    "ｾﾞ", "ｿﾞ", "ﾀﾞ", "ﾁﾞ", "ﾂﾞ", "ﾃﾞ", "ﾄﾞ", "ﾊﾞ", "ﾋﾞ", "ﾌﾞ",
    "ﾍﾞ", "ﾎﾞ", "ﾊﾟ", "ﾋﾟ", "ﾌﾟ", "ﾍﾟ", "ﾎﾟ", "ｰ", "ｧ", "ｨ",
    "ｩ", "ｪ", "ｫ", "ｬ", "ｭ", "ｮ", "ｯ", "ｳﾞ",
  ];

  const hiragana = [
    "あ", "い", "う", "え", "お", "か", "き", "く", "け", "こ",
    "さ", "し", "す", "せ", "そ", "た", "ち", "つ", "て", "と",
    "な", "に", "ぬ", "ね", "の", "は", "ひ", "ふ", "へ", "ほ",
    "ま", "み", "む", "め", "も", "や", "ゆ", "よ", "ら", "り",
    "る", "れ", "ろ", "わ", "を", "ん", "が", "ぎ", "ぐ", "げ",
    "ご", "ざ", "じ", "ず", "ぜ", "ぞ", "だ", "ぢ", "づ", "で",
    "ど", "ば", "び", "ぶ", "べ", "ぼ", "ぱ", "ぴ", "ぷ", "ぺ",
    "ぽ", "ー", "ぁ", "ぃ", "ぅ", "ぇ", "ぉ", "ゃ", "ゅ", "ょ",
    "っ", "ゔ", "ゐ", "ゑ",
  ];

  const conversionMap = {};
  for (let i = 0; i < fullWidthKatakana.length; i++) {
    conversionMap[fullWidthKatakana[i]] = hiragana[i];
  }
  for (let i = 0; i < halfWidthKatakana.length; i++) {
    conversionMap[halfWidthKatakana[i]] = hiragana[i];
  }

  const result = [];
  for (let i = 0; i < fragment.length; i++) {
    const char = fragment[i];
    if ((char === "ﾟ" || char === "ﾞ") && i > 0) {
      const combined = `${fragment[i - 1]}${char}`;
      result[result.length - 1] = conversionMap[combined] ?? combined;
    } else {
      result.push(conversionMap[char] ?? char);
    }
  }
  return result.join("");
}

function latinConvert(fragment, mode, replace) {
  return modeHandler(fragment, mode, replace);
}

function othersConvert(fragment, mode, replace) {
  return modeHandler(fragment, mode, replace);
}

export { cleanPunctuation, normalizeGana, classify, isSupported, filterUnsupported };
