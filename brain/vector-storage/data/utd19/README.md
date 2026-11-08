# UTD19 input files (not included)

UTD19 may only be used for academic / non-commercial purposes and requires sign-up, so the data is **not** stored here. Do not commit it to a public repository.

1. Sign up at https://utd19.ethz.ch (the download link arrives by email).
2. Download `utd19_u.csv` (about 7 GB) and `detectors_public.csv`.
3. From this folder run:

   ```
   python extract_torino.py utd19_u.csv detectors_public.csv
   ```

   This writes `torino.csv` and `torino_dets.csv` here. These two files are what the notebook `utd19_torino_qdrant_neo4j.ipynb` asks for.

See the main README (`../../README.md`) for the Colab steps.
