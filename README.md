VerifEye 2.0 Charter

VerifEye 2.0 is designed to be a self hosted security system with minimal cloud coupling. The reason for this ethos is for the consumer to own the full lifecycle of data, not the vendor. 

The architecture for this application is inintally designed to be local first modular monolith. This means: one backend application, one database, and one local web interface. 

The recognition pipeline is migrated from VerifEye 1.0, a school project where the core logic for this project was first designed and implemented. 

## Recognition pipeline integration test

Install the recognition dependencies into the active Python environment:

```powershell
python -m pip install -r backend/requirements-vision.txt
```

Run the pipeline against a clear image containing at least one face:

```powershell
python backend/tests/integration/recognize_image.py C:\path\to\face.jpg
```

The test uses the ArcFace model at
`~/.insightface/models/buffalo_l/w600k_r50.onnx` by default. Use
`--model-path C:\path\to\w600k_r50.onnx` when the model is elsewhere.

Successful runs write two files under `backend/test-output`:

- `<name>_annotated.jpg` shows the raw bounding box, padded bounding box,
  landmarks, and detection score.
- `<name>_recognition.json` contains those values plus the complete embedding,
  its dtype, shape, and L2 norm.

The command exits unsuccessfully if it cannot detect a face or if the aligned
image and embedding violate the pipeline contract.

## Local embedding database

Embeddings are stored locally in SQLite as normalized `float32` blobs. The
schema records model name, vector dimensions, source image, detection score,
and JSON metadata so vectors from incompatible recognition models are never
compared.

```python
from verifeye.storage import EmbeddingStore

with EmbeddingStore("backend/data/verifeye.db") as store:
    person_id = store.upsert_identity("employee-42", "Example Person")
    store.add_embedding(person_id, face.embedding, source_path="camera-1.jpg")
    matches = store.find_matches(face.embedding, min_similarity=0.4)
```

The database file and its parent directory are created on first use. Run the
storage tests with `python -m unittest discover -s backend/tests/unit`.


