# Third-Party Open Source & Model Notices

DeltaHarness (`prism-memory`) is a 100% clean-room original Python implementation built on the Python standard library (`sqlite3`, `ast`, `re`, `hashlib`, `math`). No source code from third-party memory frameworks or GPL/AGPL repositories is included.

When using optional local vector acceleration or bundled model weights, the following permissive third-party components may be loaded at runtime:

---

## 1. Multilingual E5-Small Embedding Model (`intfloat/multilingual-e5-small` / `Xenova/multilingual-e5-small`)
- **Location**: `memory_models/e5-small/` (optional local ONNX model & tokenizer)
- **License**: MIT License
- **Copyright**: Copyright (c) Liang Wang, Nan Yang, Xiaolong Huang, Binxing Jiao, Linjun Yang, Daxin Jiang, Rangan Majumder, Furu Wei (Microsoft Corporation) / Xenova ONNX conversion.

```text
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. ONNX Runtime (`onnxruntime`, Optional Dependency)
- **License**: MIT License
- **Copyright**: Copyright (c) Microsoft Corporation. All rights reserved.

---

## 3. HuggingFace Tokenizers (`tokenizers`, Optional Dependency)
- **License**: Apache License 2.0
- **Copyright**: Copyright (c) Hugging Face Inc.

---

## 4. NumPy (`numpy`, Optional Dependency)
- **License**: BSD 3-Clause License
- **Copyright**: Copyright (c) 2005-2026, NumPy Developers. All rights reserved.
