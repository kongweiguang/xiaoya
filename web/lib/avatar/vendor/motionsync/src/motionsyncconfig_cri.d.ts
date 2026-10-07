/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
/** Engine configuration for CRI. */
export interface MotionSyncEngineConfig_CRI {
    Allocator: Live2DCubismMotionSyncCore.csmMotionSync_AllocFunc;
    Deallocator: Live2DCubismMotionSyncCore.csmMotionSync_DeallocFunc;
}
export declare class MotionSyncContextConfig_CRI {
    constructor(sampleRate?: number, bitDepth?: number);
    toNativeArray(forceConversion: boolean): void;
    getNativePtr(): number;
    releaseNativeArray(): void;
    private deallocNativeArrayPtr;
    SampleRate: number;
    BitDepth: number;
    private _nativeArray;
    private _nativeArrayPtr;
}
export declare class MotionSyncAnalysisConfig_CRI {
    constructor(blendRatio?: number, smoothing?: number, audioLevelEffectRatio?: number);
    toNativeArray(forceConversion: boolean): Float32Array;
    releaseNativeArray(): void;
    private deallocNativePtr;
    BlendRatio: number;
    Smoothing: number;
    AudioLevelEffectRatio: number;
    private _nativeArray;
    private _nativeArrayPtr;
}
import * as $ from './motionsyncconfig_cri';
export declare namespace Live2DCubismMotionSyncFramework {
    const MotionSyncContextConfig_CRI: typeof $.MotionSyncContextConfig_CRI;
    type MotionSyncContextConfig_CRI = $.MotionSyncContextConfig_CRI;
}
