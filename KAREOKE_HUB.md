# 🎤 Hebrew Karaoke Project Hub

This file is the central memory for the Hebrew Karaoke AI project. AI agents should consult this file at the start of a session in this workspace.

## 🏗️ Project Architecture
- **Purpose**: A full-stack pipeline for generating Hebrew karaoke videos with synchronized lyrics.
- **Key Files**:
    - `main.py`: The entry point for the processing pipeline.
    - `timing_editor.py`: The logic for the lyric synchronization GUI.
    - `ui/timing_editor.html`: The frontend for the timing editor.
    - `config.yaml`: System configurations and API paths.

## ⚙️ AI & Hardware Specs
- **STT Pipeline**: Whisper Large v3 Turbo (via `ivrit-ai`).
- **Correction**: DictaLM-3.0-Nemotron.
- **Hardware Acceleration**: NVIDIA Quadro T2000 (CUDA enabled).
- **Audio Processing**: Demucs for vocal/instrumental separation.

## 🚀 Current Status & Tasks
- [ ] **Timing Editor Polish**: User currently has `ui/timing_editor.html` open.
- [ ] **Tests**: Several `temp_fix_*.py` files suggest recent debugging of the test suite.
- [ ] **Migration**: `migrate_to_segmented.py` indicates an ongoing shift to a new data format.

## 🛠️ Developer Notes
- **RTL Handling**: All UI elements must maintain Hebrew RTL compatibility.
- **Morphology**: Use `hebrew_nlp_utils.py` for any normalization tasks.
- **Test Results**: Consult `test_results.txt` for the latest batch run status.

---
*Created on 2026-05-15*
