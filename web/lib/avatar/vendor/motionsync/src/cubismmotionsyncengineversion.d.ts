/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
export declare class CubismMotionSyncEngineVersion {
    constructor(rawVersion: number);
    getMajor(): number;
    getMinor(): number;
    getPatch(): number;
    toString(): string;
    private _versionNumber;
    private _major;
    private _minor;
    private _patch;
}
import * as $ from './cubismmotionsyncengineversion';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncEngineVersion: typeof $.CubismMotionSyncEngineVersion;
    type CubismMotionSyncEngineVersion = $.CubismMotionSyncEngineVersion;
}
