# outlier-scan 中文简明指南

仅使用 Python 标准库的离线 CSV 异常筛查工具，支持 Python 3.10–3.14，无需 pip、密钥或联网，不收集遥测。

## 运行

在项目目录执行：

```bash
python3 scripts/anomaly_detector.py examples/synthetic_spike.csv
python3 scripts/anomaly_detector.py examples/synthetic_missing.csv --columns value --methods iqr,moving-avg --window 3
python3 scripts/anomaly_detector.py data.csv --output report.json
python3 scripts/anomaly_detector.py --test
python3 -m unittest discover -s tests -v
```

`examples/` 下全部为**人工合成测试样例**，并非真实业务数据。spike 样例的尖峰位于零基行号 20、CSV 物理行 22。

## 规则与限制

- CSV 需有表头，UTF-8（可带 BOM）。表头两端空白会被去除；空表头、重复表头、多余字段及错误引号会报错。字段不足的记录补为空值，完全空白的物理行忽略。文件为空或只有表头会报错。
- 空值、非数值字符串、NaN/Infinity 分别计入 `data_quality`，不参与计算，不会填零。无数值列时仍输出质量报告和警告。
- 自动选择至少含一个有限数值、且有限数值占“有限数值与非数值字符串”至少 80% 的列；空值与非有限值不计入该比例。也可用 `--columns` 指定。
- Z-score 和 IQR 是**全列回顾性**方法，追加数据可能改变过去判断。分别至少需要 3、4 个有限数值。零 IQR 下偏离折叠边界的点仍会被标记，常量列不会标记。
- 移动平均只用当前行之前的完整窗口，不含当前值和未来值。`--window` 默认 5，必须是 >=2 的整数。缺失、非数值、非有限值会打断连续窗口，不能把不连续数值拼接成连续时间序列。零方差基线下真实偏离会标记，相等值不标记。
- 这是**按行序**分析，不解析日期、不感知日历，也不推断漏掉的日期。请自行确认排序和采样间隔。
- `row` 为忽略空白物理行后的零基数据记录索引；`csv_line` 为包含表头的一基物理起始行，支持带换行的引号字段。
- `columns_analyzed` 只包含至少一种所选方法真正完成分析的列；样本不足会有 `insufficient_data` 警告，不可把“没分析”误当成“无异常”。
- `review` / `high_priority` 仅为**启发式复核优先级**，不代表已经证明原因、预测未来或确定数据错误。多方法并非独立证据，轻微异常也不会被称为“可解释”。
- 阈值必须有限且大于零。输出为严格 JSON，不含 NaN/Infinity；零方差比率用 null 和明确标志表示。数值运算溢出会警告，相关分析会被跳过；移动窗口仅跳过相应行，不撤销之前的结果。

成功输出报告返回 0（即使有异常或警告），输入/输出错误返回 1，参数语法错误返回 2；错误信息不显示堆栈。`--output` 会覆盖已有文件，勿指定输入路径。报告会包含输入值和列名，可能敏感，请妥善保管。

内存中读取完整 CSV；不适合无限流或超大文件。统计筛查不能替代领域判断、因果分析或预测。详细参数、API 和限制见 [英文 README](README.md)。

## 来源与许可证

改编自原 `outlier-scan` 技能脚本，原元数据声明 MIT。原始公开说明保存在 [provenance/SKILL.md](provenance/SKILL.md)，只用于来源留存，其中旧行为不适用于本版本。未复制私有状态、配置或凭据，未虚构原作者身份。许可证见 [LICENSE](LICENSE)。
