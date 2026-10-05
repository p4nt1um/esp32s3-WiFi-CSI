#ifndef CSI_CORE_H
#define CSI_CORE_H
/*
 * csi_core — 呼吸监测算法核心（平台无关纯 C，M2a）。
 *
 * 与 Python 黄金实现 analysis/csi_pipeline/dsp.py 严格同构（对拍目标：
 * 逐窗 bpm 差 ≤ 0.2、体动判定一致率 ≥ 99%）：
 *   ① Hampel@100Hz(窗15=2*7+1, β=3, reflect 边界)
 *   ② 块平均抗混叠降采样 100→10Hz
 *   ③ 去DC（101 点滑窗均值，边缘补零）
 *   ④ top-K(8) 呼吸频段能量占比子载波聚合（pow2 零填充 FFT）
 *   ⑤ 带通 0.1–0.6Hz Butterworth 4 阶 SOS 双通（零相位）
 *   ⑥ 30s 滑窗 FFT(512) 抛物线插值 + 自相关(滞后 16..100) 交叉验证
 *   ⑦ 门控：7.5s 短窗功率 vs 中位数基线(×8 上限/×0.3 下限)
 *      + FFT 峰值 ≥ 5× 带内均值 + 双法一致
 *
 * 内部使用 float32（贴近端上实际），标量参数与阈值用 double。
 * 带通 SOS 系数由 analysis/tools/export_sos.py 生成后内嵌。
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CSI_CORE_MAX_SUB 64
#define CSI_CORE_TOPK    8

typedef struct {
    int n_samples;         /* 输入样本数（100Hz 域） */
    int n_sub;             /* 子载波数（≤ CSI_CORE_MAX_SUB） */
    const int16_t *iq;     /* (n_samples × n_sub × 2) I/Q 交错 */
} csi_core_input;

typedef struct {
    double t;              /* 窗中心时刻 (s) */
    double bpm_fft;        /* FFT 峰值呼吸率 */
    double bpm_ac;         /* 自相关呼吸率 */
    double bpm;            /* (fft+ac)/2；无效窗为 NAN */
    double peak_ratio;     /* FFT 峰值 / 带内均值 */
    double power;          /* 7.5s 短窗功率（门控量） */
    int agree;             /* 双法一致 0/1 */
    int valid;             /* ⑧ 相干轨迹确认后的最终有效判定 */
    int candidate;         /* ⑧ 宽进候选（agree & ratio≥2.0 & 功率非尖峰） */
} csi_core_window;

typedef struct {
    int n_win;
    csi_core_window *win;  /* 调用方分配，容量 ≥ (n_samples/10 - 30 + 1) */
    int top_idx[CSI_CORE_TOPK];
    int n_top;
} csi_core_result;

/* 整段离线处理。返回 0 成功；<0 参数错误。 */
int csi_core_run(const csi_core_input *in, double fs_in, csi_core_result *out);

#ifdef __cplusplus
}
#endif
#endif /* CSI_CORE_H */
