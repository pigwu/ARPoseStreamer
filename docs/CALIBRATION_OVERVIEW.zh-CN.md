# 离线标定总览

语言 / Language: [English](CALIBRATION_OVERVIEW.en.md) | [简体中文](CALIBRATION_OVERVIEW.zh-CN.md)

## 这套流程解决什么问题

这套离线流程用于把 ARKit 相机位姿和机器人末端位姿对齐，并估计：

- `time_shift`：ARKit 与机器人日志之间的时间对齐量
- `initial_scale_factor`：由速度关系得到的尺度初值
- `scale_factor`：由多帧几何一致性优化得到的最终有效尺度
- `T_cam2gripper = ^gT_c`：相机到末端的固定手眼外参
- `T_base_world = ^bT_w`：ARKit world 到机器人 base 的会话级变换

核心实现位于 [pose_tracking_validator.py](../pose_tracking_validator.py)。

## 核心原理

手眼部分遵循经典 eye-in-hand 方程：

`A X = X B`

其中：

- `A` 是机器人末端的相对运动
- `B` 是相机的相对运动
- `X = ^gT_c` 是固定的相机到末端外参

与棋盘格方案不同的是，我们不从图像里估标定板位姿，而是直接使用 ARKit 输出的相机位姿。这里的 `ARKit world` 扮演会话内 `target/world frame` 的角色。

## 流程步骤

## 如何运行

示例命令：

```powershell
python pose_tracking_validator.py --mode offline --arkit-csv "uploads\20260511-225434\pose_csv__pose.csv" --sensor-csv "uploads\end_effector_pose_log (1).csv" --output-dir offline_calibration_output
```

每次运行会输出：

- `offline_calibration_output/` 下一个带时间戳的独立子目录
- `offline_calibration_result.json`
- 速度、重建误差、前后对比等图像
- 如果可用，还会输出交叉验证风险量化图

### 1. 时间同步

- 将两路时间归一化到从零开始
- 对位置对时间求导
- 将速度向量转换成标量速度 `v = ||dp/dt||_2`
- 仅在有运动的时间窗上做互相关，得到粗 `time_shift`
- 再在局部邻域内搜索，精修 `time_shift`

意义：

- 如果两路数据时间没对齐，后续刚体关系都没有意义
- 这一阶段比较的是运动模式，不是刚体位姿拟合

### 2. 尺度初值

- 在时间对齐后，对重叠运动窗比较两路速度峰值
- 用峰值比只生成一个尺度初值

注意：

- 这一步不再被当成最终真实尺度
- 因为相机中心和末端中心是刚体上的不同点，它们的线速度不必严格相等

### 3. 几何尺度优化

- 在尺度初值附近搜索候选尺度
- 对每个候选尺度：
  - 做 hand-eye 标定
  - 做 world 对齐 refinement
  - 计算末端重建绝对误差均值
- 选择多帧重建误差最小的尺度作为最终 `scale_factor`

因此，最终 `scale_factor` 表示“在当前刚体模型下最自洽的有效尺度”，而不是自动等于外部真值尺度。

### 4. 手眼标定

- 用时间对齐后的最近邻方式配对机器人帧与 ARKit 帧
- 机器人侧构造 `^bT_g`
- ARKit 侧构造 `^wT_c`，其平移乘以 `scale_factor`
- 再取逆得到 `^cT_w`，匹配 OpenCV 的 `target -> camera` 约定
- 调用 `cv2.calibrateHandEye(..., method=cv2.CALIB_HAND_EYE_TSAI)`

输出：

- `T_cam2gripper = ^gT_c`

### 5. World 对齐

- 对每一对匹配帧，计算：

  `^bT_w = ^bT_g · ^gT_c · ^cT_w`

- 对所有候选结果做平均，得到会话级 `T_base_world`

### 6. 可选的 World Refinement

- 固定 `scale_factor` 与 `T_cam2gripper`
- 只优化 `T_base_world`
- 目标是进一步降低末端绝对重建误差

在当前最新实验里，如果 refined scale 已经很合理，这一步往往收益很小。

## “预测末端位姿”是什么意思

这里的“预测”不是学习模型预测，而是通过标定得到的刚体变换链条，把 ARKit 相机位姿换算成末端位姿：

`^bT_g^pred = ^bT_w · ^wT_c · (^gT_c)^-1`

这就是把相机轨迹转换为机器人末端轨迹。

## 误差指标

### Relative Error

- 每隔 5 帧计算一次
- 衡量 `A X` 与 `X B` 的平移差
- 用来评价手眼相对运动的一致性

### Absolute Error

- 比较重建的末端位置与机器人真值末端位置
- 用来评价当前数据集上的末端重建精度

## 验证注意事项

如果不做交叉验证，当前 absolute error 更准确地说是 **in-sample reconstruction error（样本内重建误差）**。

也就是说，小误差说明当前模型能很好解释当前这批数据，但不自动等于所有参数都是真实物理真值。

## 交叉验证风险量化

项目现在支持对尺度进行交叉验证统计：

- 按时间连续分块切分训练集与验证集
- 在训练集上选择最佳 scale
- 在验证集上看重建误差 gap
- 对比速度法尺度初值与几何优化尺度的验证集收益

这样可以量化过拟合风险，而不是默认“训练误差最小就是真实”。

## 哪些量可以跨会话复用

- `T_cam2gripper`：只有在手机与末端安装关系不变时才可复用
- `T_base_world`：会话相关，ARKit world 重置后通常需要重新估计
- `time_shift / scale_factor`：会话或数据段相关

## 局限性

- ARKit world 是会话局部参考系，不是外部全局真值
- 最终 scale 是模型最自洽的有效尺度，不自动等于外部尺度真值
- 仅看样本内误差可能高估真实性能
- 如果手机离开机械臂单独采集，当前标定不能直接把那段轨迹映射到机器人 base

## 对外表述建议

建议使用如下更准确的表述：

- “有效尺度估计” 而不是 “真实物理尺度”
- “重建误差” 而不是 “独立验证误差”
- `AX = XB` 残差称为 “手眼一致性误差”
- `T_base_world` 称为 “会话级 world 对齐”
