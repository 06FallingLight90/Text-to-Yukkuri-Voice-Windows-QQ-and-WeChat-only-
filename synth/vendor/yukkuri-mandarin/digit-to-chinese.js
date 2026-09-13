const NUMBER_MAP = {
  "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
  "5": "五", "6": "六", "7": "七", "8": "八", "9": "九", ".": "点",
};

const UNIT_MAP = ["", "十", "百", "千"];

export function fractionalRead(digit) {
  return digit
    .trim()
    .split("")
    .map((d) => NUMBER_MAP[d] ?? d)
    .join("");
}

export function integralRead(digit) {
  const digitReversed = digit.split("").reverse();
  let result = "";

  for (let idx = 0; idx < digitReversed.length; idx++) {
    const d = digitReversed[idx];
    if (idx === 4 || idx === 12) {
      result = `万${result}`;
    }
    if (idx === 8) {
      if (result[0] === "万") {
        result = result.slice(1);
      }
      result = `亿${result}`;
    }
    if (d === "0") {
      if (result && !"零万亿".includes(result[0])) {
        result = `零${result}`;
      }
    } else {
      result = `${NUMBER_MAP[d]}${UNIT_MAP[idx % 4]}${result}`;
    }
  }

  if (result.startsWith("一十")) {
    result = result.slice(1);
  }

  return result;
}

export function digitToChinese(number) {
  if (!number) return "";

  if (number.endsWith("%")) {
    return `百分之${digitToChinese(number.slice(0, -1))}`;
  }

  if (number.endsWith(" ") || number.startsWith("0")) {
    return fractionalRead(number);
  }

  if (number.includes(".")) {
    const [integerPart, fractionalPart] = number.split(".", 2);
    return `${integralRead(integerPart)}点${fractionalRead(fractionalPart)}`;
  }

  if (number.length > 15 || number.length === 1) {
    return fractionalRead(number);
  }

  return integralRead(number);
}
