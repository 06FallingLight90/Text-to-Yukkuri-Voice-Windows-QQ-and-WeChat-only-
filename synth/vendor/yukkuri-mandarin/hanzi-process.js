import { pinyin } from "pinyin-pro";
import { DatabaseManager } from "./database-mngr.js";

export function hanziProcess(fragments, tokenizer, dbMngr) {
  if (!fragments || !fragments.length) return [];

  const mark = "/0";
  const markedFrag = [mark];
  for (const f of fragments) {
    markedFrag.push(f);
    markedFrag.push(mark);
  }

  const tokenized = tokenize(markedFrag, tokenizer, mark);

  const pinyinList = [];
  for (const item of tokenized) {
    if (item === mark) {
      pinyinList.push([mark]);
    } else {
      const result = pinyin(item, { type: "array", toneType: "num", v: true });
      if (!result.length) {
        pinyinList.push([item]);
      } else {
        for (const p of result) {
          let syl = p;
          if (syl.at(-1) === "0") {
            syl = `${syl.slice(0, -1)}5`;
          }
          pinyinList.push([syl]);
        }
      }
    }
  }

  const extendedMarkedFrag = extendMarkedFrag(tokenized, mark);

  if (pinyinList.length !== extendedMarkedFrag.length) {
    throw new Error(
      `处理结果出错：展开后的片段长度(${extendedMarkedFrag.length})与拼音列表长度(${pinyinList.length})不相等！`
    );
  }

  modifyConsecutiveThrees(pinyinList);
  modifyBuTone(pinyinList, extendedMarkedFrag);

  const serial = [];
  for (let i = 1; i < pinyinList.length - 1; i++) {
    const syllable = pinyinList[i][0].slice(0, -1);
    const tone = `${pinyinList[i - 1][0].slice(-1)}${pinyinList[i][0].slice(-1)}${pinyinList[i + 1][0].slice(-1)}`;
    serial.push([syllable, tone]);
  }

  if (!dbMngr) {
    dbMngr = new DatabaseManager();
  }
  const hiraganaList = dbMngr.serialSearch(serial, "");

  const result = [];
  let frag = [];
  for (let i = 0; i < serial.length; i++) {
    if (serial[i][0] === "/" && serial[i][1][1] === "0") {
      result.push(frag.join(""));
      frag = [];
    } else {
      frag.push(hiraganaList[i]);
    }
  }
  result.push(frag.join(""));

  if (result.length !== fragments.length) {
    throw new Error(
      `处理结果出错：结果的长度(${result.length})与原片段长度(${fragments.length})不相等！`
    );
  }
  return result;
}

function tokenize(fragments, tokenizer, mark = "/0") {
  if (tokenizer) {
    const result = [];
    for (const fragment of fragments) {
      if (fragment === mark) {
        result.push(mark);
      } else {
        result.push(...tokenizer.cut(fragment));
      }
    }
    return result;
  }
  return fragments;
}

function extendMarkedFrag(markedFrag, mark) {
  const result = [];
  for (const item of markedFrag) {
    if (item === mark) {
      result.push(mark);
    } else {
      result.push(...item.split(""));
    }
  }
  return result;
}

function modifyConsecutiveThrees(pinyinList) {
  if (pinyinList.length < 2) return;
  let flag = 0;
  for (let i = 0; i < pinyinList.length - 1; i++) {
    const curr = pinyinList[i][0];
    const next = pinyinList[i + 1][0];
    if (curr.endsWith("3") && next.endsWith("3")) {
      if (flag === 0) {
        flag = 1;
      } else if (flag === 1) {
        flag = 2;
      } else if (flag === 2) {
        pinyinList[i - 1][0] = `${pinyinList[i - 1][0].slice(0, -1)}3`;
        flag = 1;
      }
      pinyinList[i][0] = `${pinyinList[i][0].slice(0, -1)}2`;
    } else {
      if (flag !== 0) flag = 0;
    }
  }
}

function modifyBuTone(pinyinList, extendedMarkedFrag) {
  if (pinyinList.length < 2) return;
  for (let i = 0; i < pinyinList.length - 1; i++) {
    if (
      extendedMarkedFrag[i] === "不" &&
      extendedMarkedFrag[i + 1] !== "不" &&
      pinyinList[i][0] === "bu4" &&
      pinyinList[i + 1][0].endsWith("4")
    ) {
      pinyinList[i][0] = "bu2";
    }
  }
}

export { tokenize, extendMarkedFrag, modifyConsecutiveThrees, modifyBuTone };