@echo off
:: ESP-IDF v5.5.2 wrapper for non-interactive use (DOSKEY macros don't work in cmd /c)
:: Usage from bash: cmd //c "E:\ESP32Project\WIFI-CSI\idf.bat <idf.py args>"
:: Current working directory is inherited - cd to the project first.
:: MSYSTEM must be cleared: inherited Git Bash identity makes idf.py refuse to run.
:: --no-ccache: ccache flakily fails to exec xtensa gcc on this machine (AV interference);
::              idf.py appends its own CCACHE_ENABLE after user -D, so the flag is the only reliable switch.
set "MSYSTEM="
call E:\Espressif\idf_cmd_init.bat esp-idf-e28d566c619822639aa813220bab1f0b >nul 2>&1
python E:\Espressif\frameworks\esp-idf-v5.5.2\tools\idf.py --no-ccache %*
