/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { CubismModelSettingJson } from '@framework/cubismmodelsettingjson';
import { csmString } from '@framework/type/csmstring';
import { csmVector } from '@framework/type/csmvector';
export const FileReferences = 'FileReferences';
export const MotionSync = 'MotionSync';
export class CubismModelMotionSyncSettingJson extends CubismModelSettingJson {
    constructor(buffer, size) {
        super(buffer, size);
        this._motionSyncFilePath = this.getJson()
            .getRoot()
            .getValueByString(FileReferences)
            .getValueByString(MotionSync)
            .getRawString();
    }
    getMotionSyncFileName() {
        return this._motionSyncFilePath;
    }
    getMotionSyncSoundFileList() {
        const list = new csmVector();
        for (let index = 0; index < this.getMotionGroupCount(); index++) {
            const groupName = this.getMotionGroupName(index);
            for (let listIndex = 0; listIndex < this.getMotionCount(groupName); listIndex++) {
                const fileName = this.getMotionSoundFileName(groupName, listIndex);
                // ファイル名が空であれば無視する。
                if (!fileName || fileName.length < 1) {
                    continue;
                }
                list.pushBack(new csmString(fileName));
            }
        }
        return list;
    }
}
// Namespace definition for compatibility.
import * as $ from './cubismmodelmotionsyncsettingjson';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismModelMotionSyncSettingJson = $.CubismModelMotionSyncSettingJson;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));
