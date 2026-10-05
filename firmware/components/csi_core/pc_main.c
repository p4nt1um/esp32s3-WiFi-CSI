/* PC 端对拍工具：读 esp-csi CSV → csi_core 管线 → 输出逐窗结果 CSV。
 * 编译（Git Bash，分析环境 venv）：
 *   analysis/.venv/Scripts/python.exe -m ziglang cc -O2 src/csi_core.c pc_main.c -lm -o build/csi_pc.exe
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "csi_core.h"

#define LINE_BUF 8192

static int16_t *s_iq = NULL;
static size_t s_cap = 0, s_cnt = 0;
static int s_nsub = 0;

static void push_iq(int v)
{
    if (s_cnt == s_cap) {
        s_cap = s_cap ? s_cap * 2 : 64 * 104;
        s_iq = realloc(s_iq, sizeof(int16_t) * s_cap);
        if (!s_iq) { fprintf(stderr, "oom\n"); exit(2); }
    }
    s_iq[s_cnt++] = (int16_t)v;
}

/* 解析一行 CSI_DATA：data 字段为行尾的 "[v,v,...]"，按子载波 I/Q 交错收集 */
static int parse_line(const char *line)
{
    const char *p = strstr(line, ",\"[");
    if (!p) return -1;
    if (s_nsub == 0) {                    /* 用 data 值个数推子载波数 */
        int n = 0;
        for (const char *r = p + 3; *r && *r != ']'; r++)
            if (*r == ',') n++;
        s_nsub = (n + 1) / 2;
    }
    p += 3;
    while (*p && *p != ']') {
        char *end;
        long v = strtol(p, &end, 10);
        if (end == p) break;
        push_iq((int)v);
        p = end;
        if (*p == ',') p++;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <in.csv> <out_windows.csv> [fs]\n", argv[0]);
        return 1;
    }
    double fs = argc > 3 ? atof(argv[3]) : 100.0;

    FILE *f = fopen(argv[1], "r");
    if (!f) { perror(argv[1]); return 1; }
    char line[LINE_BUF];
    while (fgets(line, sizeof(line), f))
        if (strncmp(line, "CSI_DATA,", 9) == 0)
            parse_line(line);
    fclose(f);

    if (s_nsub <= 0 || s_cnt / (size_t)(s_nsub * 2) < 64) {
        fprintf(stderr, "not enough data (sub=%d samples=%zu)\n",
                s_nsub, s_nsub ? s_cnt / (size_t)(s_nsub * 2) : 0);
        return 1;
    }

    csi_core_input in = {
        .n_samples = (int)(s_cnt / (size_t)(s_nsub * 2)),
        .n_sub = s_nsub,
        .iq = s_iq,
    };
    int max_win = in.n_samples / 10 - 30 + 2;
    csi_core_result res;
    memset(&res, 0, sizeof(res));
    res.win = calloc((size_t)max_win, sizeof(csi_core_window));
    if (!res.win) { fprintf(stderr, "oom\n"); return 2; }

    int rc = csi_core_run(&in, fs, &res);
    if (rc != 0) { fprintf(stderr, "csi_core_run rc=%d\n", rc); return 1; }

    FILE *o = fopen(argv[2], "w");
    if (!o) { perror(argv[2]); return 1; }
    fprintf(o, "t,bpm_fft,bpm_ac,agree,peak_ratio,power,valid,bpm\n");
    for (int w = 0; w < res.n_win; w++) {
        const csi_core_window *r = &res.win[w];
        char bpm_str[32];
        if (r->valid) snprintf(bpm_str, sizeof(bpm_str), "%.6g", r->bpm);
        else          snprintf(bpm_str, sizeof(bpm_str), "nan");
        fprintf(o, "%.6g,%.6g,%.6g,%d,%.6g,%.6g,%d,%s\n",
                r->t, r->bpm_fft, r->bpm_ac, r->agree, r->peak_ratio, r->power,
                r->valid, bpm_str);
    }
    fclose(o);
    printf("windows=%d sub=%d samples=%d top_idx=", res.n_win, s_nsub, in.n_samples);
    for (int r = 0; r < res.n_top; r++) printf("%s%d", r ? "," : "", res.top_idx[r]);
    printf("\n");
    return 0;
}
