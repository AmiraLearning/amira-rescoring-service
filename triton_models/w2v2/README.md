Place your exported ONNX or TensorRT plan here as `model.onnx` or `model.plan`.

Expected I/O:
- INPUT__0: FP32 mono PCM at 16kHz, shape [N] or [B, N]
- OUTPUT__0: FP32 logits, shape [B, T, V]

Update config.pbtxt dims to match your export.

