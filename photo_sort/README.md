libs: pip install pillow piexif exifread pillow-heif geopy
start:
# пробный прогон, ничего не трогает, только показывает куда что пойдёт:
python3 sort_photos.py --src ~/Фото/iPhone --dst ~/Фото/Сортировано --dry-run

# копировать (оригиналы остаются):
python3 sort_photos.py --src ~/Фото/iPhone --dst ~/Фото/Сортировано

# переместить:
python3 sort_photos.py --src ~/Фото/iPhone --dst ~/Фото/Сортировано --move