# mIHC + Coregistration: Docker package

Two separate images (the mIHC one is small; the coregistration one carries Java + VALIS and is large).

| App | Folder | Port | Needs |
|---|---|---|---|
| mIHC Easy Analysis | `mihc/` | 8502 | any machine; reads QuPath CSVs |
| Coregistration Easy | `coreg/` | 8501 | RAM: ~24 GB per parallel job (the app picks 1-4 jobs from the RAM it sees) |

## Build and run (on the server)
Edit the two `CHANGE` paths in `docker-compose.yml` (slides folder, CSV folder), then:
```bash
docker compose build
docker compose up -d
```
Open `http://<server>:8501` (coregistration) and `http://<server>:8502` (mIHC).

## Folders inside the containers
Type these in the app boxes:
- mIHC: project folder `/data`; results are downloaded from the page, nothing is saved on the server
- Coregistration: slide folders under `/data/...`; results go to `<slide folder's parent>/Coregistration` by default (written back into the mounted slides folder)

Job progress lives in the `coreg-runs` volume, so **Resume** still works after a restart or redeploy.

## Pushing to a registry (for the team)
```bash
docker tag farcast/mihc-easy:1.0  <registry>/mihc-easy:1.0   && docker push <registry>/mihc-easy:1.0
docker tag farcast/coreg-easy:1.0 <registry>/coreg-easy:1.0  && docker push <registry>/coreg-easy:1.0
```
If you push, change `image:` in `docker-compose.yml` to the registry names and use `docker compose pull`.

## Notes
- The app has no login. Keep the ports on the internal network or put the lab's reverse proxy / SSO in front.
- "Browse" buttons are off in Docker (no desktop); paths are typed.
- The coregistration image downloads the Bio-Formats jars at build time. If the build host is offline the step is skipped and the first job downloads them instead.
- Source copies of the apps live in `mihc/` and `coreg/`; re-copy from the project folders before rebuilding after any code change.
- Not yet built or run by me: Docker was not running on the PC this was prepared on. Run `docker compose build` once and open both pages before announcing it.
