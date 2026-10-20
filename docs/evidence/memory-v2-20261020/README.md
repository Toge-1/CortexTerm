# 2026-10-20 Memory V2 证据包

- [交互式查看](pilot/index.html)：离线 HTML，内嵌数据，无外部请求。
- `pilot/manifest.json`：五组比较的参数与冻结提交 `d0824cc`。
- `pilot/cases.frozen.json`：当次实际使用的数据与原始断言。
- `pilot/runs.jsonl` / `report.json`：原始结果，未覆盖。
- `pilot/runs.audited.jsonl` / `report.audited.json`：统一修正过严断言后的重判，没有重新生成代码。
- `pilot/*.memories.json`：真实模型提取的经验与来源引文。
- `repo-calibration/`：8 步预算的第一轮真实仓库回放；各组均 0/2，保留失败记录。
- `repo-replay/`：16 步、4000 输出上限、统一提供文件清单的第二轮；冻结提交 `9820b65`。
- `repo-replay/*/provenance.json`：具体 Git 版本、原有回归测试、坏版本失败/已知修复版本通过的校验。
- `extraction-recheck.json`：关闭后台提取 thinking 后，两项此前截断输入的复测。
- `sha256.json`：原始归档文件校验值，不包含此 README、HTML 及清单自身。

这些实验使用当前配置的真实 DeepSeek API。正文、源码与工具输出是公开事件；没有保存 API key 或思考内容。服务端 usage 原样保留，缓存输入单列。小样本和真实仓库回放的适用边界见 [评测说明](../../memory-v2-evaluation.md)。

HTML 在生成后通过离线语法与数据检查；内置浏览器拒绝 file 协议，未做截图验收。
