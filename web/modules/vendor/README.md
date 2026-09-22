# Vendored browser runtime

固定版本的第三方浏览器运行时，随仓库分发到 `static/modules/vendor/`；这些文件不是产品自有代码，除扩展名改写外不得手改内容。

## i18next 26.4.2

- 版本：`26.4.2`
- 上游：`npm i18next`（https://www.npmjs.com/package/i18next）
- 原文件：`dist/esm/i18next.js`
- 本地文件：`static/modules/vendor/i18next-26.4.2.mjs`
- SHA-256：`4c04240958fbbc854a9ac7d983dccba560c4a19a4f80dd87cddd4e82142f39da`
- 许可证：MIT，见 `i18next-LICENSE`

### 更新方式

```bash
cd /tmp && npm pack i18next@<version> && tar xzf i18next-<version>.tgz
cp package/dist/esm/i18next.js <repo>/deploy/local_web/static/modules/vendor/i18next-<version>.mjs
cp package/LICENSE <repo>/deploy/local_web/static/modules/vendor/i18next-LICENSE
shasum -a 256 <repo>/deploy/local_web/static/modules/vendor/i18next-<version>.mjs
```

更新后同步本 README 的版本与 SHA-256，并确认：

- 文件自包含（无 `import` 语句），浏览器无需 CDN 或运行时下载；
- 文件不含 CJK 字符，不影响 `tools/check_i18n.py`；
- 旧版本文件与对应 README 记录一并替换，不保留多版本共存。
