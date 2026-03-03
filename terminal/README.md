# Single video
python main.py https://youtu.be/KvZJQAj3cJ4

# By video ID, with French + timestamps
python main.py KvZJQAj3cJ4 --lang fr --timestamps

# Print to terminal instead of saving
python main.py KvZJQAj3cJ4 --stdout

# See what languages are available
python main.py KvZJQAj3cJ4 --list-langs

# Batch from a file (one URL/ID per line)
python main.py --file urls.txt --output ./out

# Custom output folder, overwrite existing files
python main.py KvZJQAj3cJ4 --output ~/transcripts --force


python main.py --channel tahaessou94

python main.py --channel tahaessou94 --max-videos 10

python main.py --file urls.txt