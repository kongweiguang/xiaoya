/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { CubismModelSettingJson } from '@framework/cubismmodelsettingjson';
import { csmString } from '@framework/type/csmstring';
import { csmVector } from '@framework/type/csmvector';
export declare const FileReferences: string;
export declare const MotionSync: string;
export declare class CubismModelMotionSyncSettingJson extends CubismModelSettingJson {
    constructor(buffer: ArrayBuffer, size: number);
    getMotionSyncFileName(): string;
    getMotionSyncSoundFileList(): csmVector<csmString>;
    private _motionSyncFilePath;
}
import * as $ from './cubismmodelmotionsyncsettingjson';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismModelMotionSyncSettingJson: typeof $.CubismModelMotionSyncSettingJson;
    type CubismModelMotionSyncSettingJson = $.CubismModelMotionSyncSettingJson;
}
