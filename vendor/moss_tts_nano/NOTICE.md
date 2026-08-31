# Vendored: MOSS-TTS-Nano ONNX runtime

Source: https://github.com/OpenMOSS/MOSS-TTS-Nano at commit
`7f75b9eb8818f929560459ce8669909dece85975` (2026-08-30). Copyright the OpenMOSS
team / MOSI.AI, Apache License 2.0 — see `LICENSE` in this directory.

Files: `ort_cpu_runtime.py`, `onnx_tts_runtime.py`, `text_normalization_pipeline.py`,
`tts_robust_normalizer_single_script.py` (unchanged except as noted).

Changes made for vox (all marked `vox:` in the source):

- `onnx_tts_runtime.py`: dropped the `torch`/`torchaudio` imports; `_load_reference_audio`
  now reads with `soundfile` and requires the codec's sample rate (vox converts clips when
  a voice is added). `DEFAULT_OUTPUT_DIR` is a temp dir. `ensure_browser_onnx_model_dir`
  downloads the weights into whatever directory it is given instead of only the repo
  default. Imports are package-relative.
- `text_normalization_pipeline.py`: package-relative import.

The model weights are not vendored; they are downloaded from Hugging Face on first use
(`OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX`, `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX`,
Apache-2.0, about 730 MB) into `~/.cache/vox/models/`.
