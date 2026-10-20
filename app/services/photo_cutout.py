"""
Lot 87 — détourage sur NOTRE serveur (gratuit) : modèle IS-Net (« isnet-general-use », licence Apache 2.0, celui de
l'outil libre rembg) exécuté avec onnxruntime.

Lancé dans un PROCESSUS SÉPARÉ (python -m app.services.photo_cutout entrée sortie modèle) : le modèle prend
plusieurs centaines de Mo de mémoire, rendus dès la fin du détourage (le serveur héberge aussi d'autres applications).
"""
import sys

SIZE = 1024
MEAN = (0.5, 0.5, 0.5)
STD = (1.0, 1.0, 1.0)


def cutout(source_path: str, target_path: str, model_path: str) -> None:
    import numpy as np
    import onnxruntime as ort
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(Image.open(source_path)).convert("RGB")
    small = image.resize((SIZE, SIZE), Image.LANCZOS)
    array = np.array(small).astype(np.float32)
    array = array / max(float(array.max()), 1e-6)
    normalized = np.stack([(array[:, :, c] - MEAN[c]) / STD[c] for c in range(3)])[None].astype(np.float32)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])
    prediction = session.run(None, {session.get_inputs()[0].name: normalized})[0][:, 0, :, :]
    low, high = float(prediction.min()), float(prediction.max())
    prediction = (prediction - low) / max(high - low, 1e-6)
    mask = Image.fromarray((prediction[0] * 255).astype("uint8")).resize(image.size, Image.LANCZOS)
    result = image.convert("RGBA")
    result.putalpha(mask)
    result.save(target_path, "PNG")


if __name__ == "__main__":
    cutout(sys.argv[1], sys.argv[2], sys.argv[3])
