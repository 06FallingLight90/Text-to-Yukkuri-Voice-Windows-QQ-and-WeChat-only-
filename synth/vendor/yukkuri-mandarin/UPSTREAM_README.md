# Yukkuri-Mandarin-JS

[[在线 Demo (Vercel)]](https://yukkuri.xiaym.top/) [[在线 Demo (Cloudflare)]](https://yukkuri-cf.xiaym.top/)

**此项目的大部分内容由 LLM 辅助构建。**

油库里语音合成器，支持中、英文快速合成。

## 构建

```shell
pnpm i
pnpm --filter yukkuri-mandarin-app build
```

在构建完成后，将 `app/dist` 中的内容部署到 Web 服务器上即可使用。

## 鸣谢

- [Yukkuri-Mandarin \(Python 库\)](https://github.com/wubzbz/Yukkuri-Mandarin) - *本项目包含了此项目的 JavaScript 翻译版本* - MIT
- [pinyin-pro](https://github.com/zh-lx/pinyin-pro) - MIT
- [cmu-pronouncing-dictionary](https://github.com/words/cmu-pronouncing-dictionary) - ISC
- [aquestalk.js](https://github.com/y52en/aquestalk.js) - MIT

## 协议

MIT License.
