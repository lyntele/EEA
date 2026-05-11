# probe 脚本目录

ad-hoc 验收脚本，生命周期规则:

- 每个 WU 完成 + 用户确认验收后，在下一 WU 开始前降级或删除。
- 有长期价值的脚本移到 `rulebook/tests/`。
- 仅本轮验收用的脚本删除。
- 不允许长期累积“以防万一”的死代码。

## 当前在用

- `wuv2_4_unit_bucket_convergence.py`: WUv2-4 结构化 repair-card retrieval bucket 单元级汇合 probe。

## 已删除归档

- 暂无。后续删除时记录脚本名和 commit hash，便于回溯。
