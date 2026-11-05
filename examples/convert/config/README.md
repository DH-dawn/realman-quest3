# 数据集转换配置

`conversion_config.json`由项目的`init-config`命令从公共模板创建，不覆盖已有文件。
实际JSON属于本机配置。

- `input`：episode、session或集合目录，首次使用需填写。
- `output`：新的LeRobot数据集目录，不能已存在。
- `repo_id`：数据集名称。
- `fps`、`image_size`、`min_frames`：采样率、输出图像尺寸与最短片段帧数。
- `max_gripper_age`：夹爪反馈最大有效年龄，单位秒。
- `task`：可选的任务文字覆盖值。
- `action_source`：`sent-target`为已发送目标标签，`next-state`为下一帧实际状态标签。
- `dry_run`：默认true，仅检查数据；false执行转换。

相对输入、输出路径以本目录为基准。默认输出`../../../datasets/rm65_dual_aligned`指向项目的datasets目录。
