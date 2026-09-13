import { digitToChinese } from "./digit-to-chinese.js";

const SYMBOL = { "+": "加", "=": "等于", "#": "井" };

function isNum(char) {
  return char && char.length === 1 && char >= "0" && char <= "9";
}

function toHalfWidth(char) {
  const code = char.charCodeAt(0);
  if (code >= 0xff01 && code <= 0xff5e) {
    return String.fromCharCode(code - 0xfee0);
  }
  return char;
}

export function preProcess(sentence) {
  sentence = [...sentence].map(toHalfWidth).join("");
  const result = [];
  let numBasket = [];

  for (let i = 0; i < sentence.length; i++) {
    const char = sentence[i];
    const nextIdx = i + 1;

    if (isNum(char)) {
      numBasket.push(char);
    } else {
      const replaced = SYMBOL[char] ?? char;
      if (numBasket.length) {
        if (replaced === " " || replaced === "%") {
          if (replaced === " ") {
            if (sentence[nextIdx] === "%") {
              numBasket.push("%");
              result.push(digitToChinese(numBasket.join("")));
              numBasket = [];
              i += 1;
            } else {
              result.push(digitToChinese(numBasket.join("")));
              numBasket = [];
              result.push(" ");
            }
          } else {
            numBasket.push("%");
            result.push(digitToChinese(numBasket.join("")));
            numBasket = [];
          }
        } else if (
          (replaced === "," || replaced === "，") &&
          nextIdx < sentence.length &&
          isNum(sentence[nextIdx])
        ) {
        } else if (
          replaced === "." &&
          nextIdx < sentence.length &&
          isNum(sentence[nextIdx])
        ) {
          numBasket.push(replaced);
        } else {
          result.push(digitToChinese(numBasket.join("")));
          numBasket = [];
          result.push(replaced);
        }
      } else {
        if (
          char === "-" &&
          nextIdx < sentence.length &&
          isNum(sentence[nextIdx]) &&
          !(i > 0 && isNum(sentence[i - 1]))
        ) {
          result.push("负");
        } else {
          result.push(replaced);
        }
      }
    }
  }

  if (numBasket.length) {
    result.push(digitToChinese(numBasket.join("")));
  }

  return result.join("");
}
