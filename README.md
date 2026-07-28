# My Fork
UI is overhauled, with a sidebar, new insights/analytics, and better UI/UX (imo)

Just to list a few improvements:
-Tells you, on average, how much of a song you get through before skipping
-Tells you your listening patterns by the hour, weekday, and month
-Tells you the days you discovered the most new music
-Shows you graphs of how much you listened to a song over time
-Allows you to import multiple of your spotify History .json files at a time
-and more! see screenshots in the `assets` folder

# Original readme below
# Spotify Stats Tracker
### If you found [this repository](https://github.com/TzurSoffer/SpotifyStatsTracker) useful, please give it a ⭐!.
A web application that allows users to track and analyze their Spotify listening habits and statistics **without Spotify Premium**.

## Features

- View your top songs.
- View your top artists.
- See your listening history.
- Track your spotify recently listened in real time
- Import Spotify data export
- Import musicolet pro exports
- Automatically import files in the 'auto-import folder' with optional filtering

## Installation

1. Clone the repository:
```bash
git clone https://github.com/TzurSoffer/SpotifyStatsTracker
cd SpotifyStatsTracker
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Run the Application

### Using Docker

Use this docker-compose file:
```docker
version: '3.8'

services:
  spotify-tracker:
    image: mepro3/spotify-tracker
    ports:
      - "5000:5000"
    volumes:
      - ./Database:/app/Database/Users
      - ./secrets:/app/secrets
      - ./autoImport:/app/autoImport  #< files put in this folder will be imported automatically
    environment:
      - FLASK_APP=app.py
      - PYTHONUNBUFFERED=1
      - TZ=America/Los_Angeles        #< don't forget to change this or you will get the wrong times for songs
      # - IMPORT_KEYWORD=Weekly       #< Uncomment to apply a filter to what files get auto-imported (only files containing this will be imported)
      # - FLASK_DEBUG=1               #< To get more detailed logs from Flask (provide this when opening an issue)
    restart: always
```

### Then you can run `docker compose up -d` and the app should start on `http://127.0.0.1:5000` or `http://yourIp:5000`

### To update the container if an update is available, run `docker compose pull`

### Local Development

1. Start the app:
```bash
python app.py
```

2. Open the app in your browser:
```text
http://127.0.0.1:5000
```
or whatever your IP is

3. Open the app in your browser:
```text
http://127.0.0.1:5000
```

**Note:** The Docker container persists data in the `Database/` directory on your host machine.

## License

This project is licensed under the `Soffer Non-Commercial Source-Available License 1.1` - see the LICENSE file for details.

## Support

For support, please open an issue on the GitHub repository or contact me.
