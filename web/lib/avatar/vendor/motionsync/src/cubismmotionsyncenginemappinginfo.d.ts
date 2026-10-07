/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmString } from '@framework/type/csmstring';
import { csmVector } from '@framework/type/csmvector';
export declare const MappingInfoStructSize = 6;
export declare class CubismMotionSyncEngineMappingInfo {
    constructor(audioParameterId: csmString, modelParameterIds: csmVector<csmString>, modelParameterValues: csmVector<number>, scale: number, enabled: boolean);
    toNativeArray(forceConversion: boolean): Float32Array;
    releaseNativeArray(): void;
    getAudioParameterId(): csmString;
    getModelParameterIds(): csmVector<csmString>;
    getModelParameterValues(): csmVector<number>;
    getScale(): number;
    getEnabled(): number;
    private deallocNativeArrayPtr;
    private _audioParameterId;
    private _modelParameterIds;
    private _modelParameterValues;
    private _scale;
    private _enabled;
    private _nativeArray;
    private _nativeArrayPtr;
}
import * as $ from './cubismmotionsyncenginemappinginfo';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncEngineMappingInfo: typeof $.CubismMotionSyncEngineMappingInfo;
    type CubismMotionSyncEngineMappingInfo = $.CubismMotionSyncEngineMappingInfo;
}
