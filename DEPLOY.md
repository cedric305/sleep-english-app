# Cloud Deploy

## Recommended

Use a Docker-based host with persistent storage. This app stores:

- saved audio clips in `data/clips/`
- sentence library in `data/library.json`
- word library in `data/words.json`

Without a volume, those files may disappear after a restart or redeploy.

## Railway

1. Push this project to GitHub.
2. Create a new Railway project from that GitHub repo.
3. Railway will detect the `Dockerfile` and build the app automatically.
4. Add a volume and mount it to `/data`.
5. Set the app's public port to `8080` if Railway does not detect it automatically.
6. Deploy.

Environment variables:

- `APP_DATA_DIR=/data`
- `PORT=8080`

## Render

1. Push this project to GitHub.
2. Create a new Web Service from the repo.
3. Choose Docker as the runtime.
4. Add a persistent disk and mount it to `/data`.
5. Add environment variables:
   - `APP_DATA_DIR=/data`
   - `PORT=8080`
6. Deploy.

## Notes

- `ffmpeg` is already installed in the Docker image.
- The app serves saved mp3 files from the mounted data directory.
- If you later want video summary back, you will need a cloud-accessible LLM provider. Running Ollama on your home computer will not work reliably after cloud deployment.
