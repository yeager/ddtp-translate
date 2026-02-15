#!/usr/bin/env python3
"""Merge .po translations into a .desktop.in template to produce a .desktop file."""
import sys, os, re, glob

def parse_po(po_file):
    """Extract msgid→msgstr pairs from a .po file."""
    translations = {}
    msgid = msgstr = None
    in_msgid = in_msgstr = False
    
    with open(po_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('msgid "'):
                msgid = line[7:-1]
                in_msgid = True
                in_msgstr = False
            elif line.startswith('msgstr "'):
                msgstr = line[8:-1]
                in_msgstr = True
                in_msgid = False
            elif line.startswith('"') and line.endswith('"'):
                if in_msgid:
                    msgid += line[1:-1]
                elif in_msgstr:
                    msgstr += line[1:-1]
            else:
                if msgid and msgstr:
                    translations[msgid] = msgstr
                in_msgid = in_msgstr = False
                msgid = msgstr = None
        if msgid and msgstr:
            translations[msgid] = msgstr
    return translations

def merge(template, po_dir, output):
    """Read .desktop.in, merge translations, write .desktop."""
    lines = open(template, 'r', encoding='utf-8').readlines()
    
    # Collect all .po files
    po_files = glob.glob(os.path.join(po_dir, '*.po'))
    lang_translations = {}
    for po in po_files:
        lang = os.path.splitext(os.path.basename(po))[0]
        lang_translations[lang] = parse_po(po)
    
    with open(output, 'w', encoding='utf-8') as out:
        for line in lines:
            stripped = line.strip()
            # Check for translatable keys: _Name=, _Comment=, _Keywords=, _GenericName=
            m = re.match(r'^_(\w+)=(.*)$', stripped)
            if m:
                key, value = m.group(1), m.group(2)
                out.write(f'{key}={value}\n')
                # Add translations
                for lang in sorted(lang_translations.keys()):
                    trans = lang_translations[lang].get(value, '')
                    if trans:
                        out.write(f'{key}[{lang}]={trans}\n')
            else:
                out.write(line)

if __name__ == '__main__':
    if len(sys.argv) != 4:
        print(f'Usage: {sys.argv[0]} template.desktop.in po_dir output.desktop')
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2], sys.argv[3])
