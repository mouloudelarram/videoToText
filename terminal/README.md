# Single video
python main.py https://youtu.be/5aOF-RIZS5c

# By video ID, with French + timestamps
python main.py 5aOF-RIZS5c --lang fr --timestamps

# Print to terminal instead of saving
python main.py 5aOF-RIZS5c --stdout

# See what languages are available
python main.py 5aOF-RIZS5c --list-langs

# Batch from a file (one URL/ID per line)
python main.py --file urls.txt --output ./out

# Custom output folder, overwrite existing files
python main.py 5aOF-RIZS5c --output ~/transcripts --force

# for CHannels
python main.py --channel googlecloudtech

python main.py --channel googlecloudtech --max-videos 10

python main.py --file urls.txt