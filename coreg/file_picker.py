import sys
import tkinter as tk
from tkinter import filedialog

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "file"
    title = sys.argv[2] if len(sys.argv) > 2 else "Select"
    
    root = tk.Tk()
    root.attributes("-topmost", True)
    root.withdraw()
    root.focus_force()
    
    if mode == "dir":
        path = filedialog.askdirectory(title=title)
    else:
        path = filedialog.askopenfilename(
            title=title,
            filetypes=[("CZI files", "*.czi"), ("All files", "*.*")]
        )
    root.destroy()
    if path:
        print(path.replace("/", "\\"))

if __name__ == "__main__":
    main()
